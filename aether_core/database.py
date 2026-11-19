"""SQLite brain: WAL-mode storage for the code graph, FTS5 search, scars, telemetry and events.

All public methods are thread-safe (a single connection guarded by an RLock), which lets the
MCP server, the web server and background watchers share one ``AetherDB`` instance.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

# ``schema_version`` is the on-disk format contract (bumped only for breaking changes). ``schema_revision``
# counts additive changes (new tables/indexes) that older readers can safely ignore.
SCHEMA_VERSION = 1
SCHEMA_REVISION = 2  # v0.2: session_checkpoints, budget_usage

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    root_path TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    indexed_at REAL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    language TEXT NOT NULL,
    sha1 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    line_count INTEGER NOT NULL,
    token_estimate INTEGER NOT NULL,
    indexed_at REAL NOT NULL,
    UNIQUE(repo_id, path)
);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualname TEXT NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    docstring TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    start_line INTEGER NOT NULL DEFAULT 1,
    end_line INTEGER NOT NULL DEFAULT 1,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_id);
CREATE INDEX IF NOT EXISTS idx_nodes_kind_name ON nodes(kind, name);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    src_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    src_node_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    dst_name TEXT NOT NULL,
    dst_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    dst_node_id INTEGER REFERENCES nodes(id) ON DELETE SET NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_file_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_file_id);
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);

CREATE TABLE IF NOT EXISTS scars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    repo_name TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    error_summary TEXT NOT NULL,
    root_cause TEXT NOT NULL DEFAULT '',
    fix_applied TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'high',
    source TEXT NOT NULL DEFAULT 'manual',
    commit_sha TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    last_hit_at REAL
);
CREATE INDEX IF NOT EXISTS idx_scars_path ON scars(file_path);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    raw_tokens INTEGER NOT NULL DEFAULT 0,
    compressed_tokens INTEGER NOT NULL DEFAULT 0,
    saved_tokens INTEGER NOT NULL DEFAULT 0,
    saved_usd REAL NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    origin TEXT NOT NULL,
    nodes TEXT NOT NULL DEFAULT '[]',
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS security_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    rule TEXT NOT NULL,
    severity TEXT NOT NULL,
    file_path TEXT NOT NULL DEFAULT '',
    line INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    created_at REAL NOT NULL,
    file_count INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    manifest_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS swarm_runs (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS session_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    session TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL,
    files TEXT NOT NULL DEFAULT '[]',
    pending TEXT NOT NULL DEFAULT '[]',
    decisions TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_created ON session_checkpoints(created_at);

CREATE TABLE IF NOT EXISTS budget_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session TEXT NOT NULL,
    tool TEXT NOT NULL,
    tokens INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_budget_session ON budget_usage(session);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name, qualname, signature, docstring, body, path,
    node_id UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2 tokenchars ''_'''
);
"""

_IDENT_SPLIT = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


_CHUNK = re.compile(r"[A-Za-z0-9]+")


@lru_cache(maxsize=65536)
def _split_chunk(chunk: str) -> tuple[str, ...]:
    return tuple(p.lower() for p in _IDENT_SPLIT.findall(chunk) if p)


def split_identifier(text: str) -> list[str]:
    """Split ``camelCase``/``snake_case``/``kebab-case`` identifiers into lowercase words."""
    words: list[str] = []
    for chunk in _CHUNK.findall(text):
        words.extend(_split_chunk(chunk))
    return words


def normalize_rel_path(path: str) -> str:
    """Normalize a user-supplied relative path to forward slashes without a leading ``./``."""
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def expand_for_fts(text: str) -> str:
    """Augment text with its identifier sub-words so FTS matches ``getUser`` for ``user``.

    Only compound identifiers contribute extra words (plain words are already in ``text``)."""
    if not text:
        return ""
    extra: list[str] = []
    seen: set[str] = set()
    for chunk in _CHUNK.findall(text):
        if chunk in seen:
            continue
        seen.add(chunk)
        parts = _split_chunk(chunk)
        if len(parts) > 1:
            extra.extend(parts)
    return f"{text} {' '.join(extra)}" if extra else text


@dataclass(slots=True)
class FileRecord:
    id: int
    repo_id: int
    repo_name: str
    path: str
    language: str
    sha1: str
    size_bytes: int
    line_count: int
    token_estimate: int


@dataclass(slots=True)
class NodeRecord:
    id: int
    file_id: int
    repo_name: str
    path: str
    kind: str
    name: str
    qualname: str
    signature: str
    docstring: str
    body: str
    start_line: int
    end_line: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScarRecord:
    id: int
    fingerprint: str
    repo_name: str
    file_path: str
    symbol: str
    error_summary: str
    root_cause: str
    fix_applied: str
    severity: str
    source: str
    commit_sha: str
    created_at: float
    hit_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repo": self.repo_name,
            "file_path": self.file_path,
            "symbol": self.symbol,
            "error_summary": self.error_summary,
            "root_cause": self.root_cause,
            "fix_applied": self.fix_applied,
            "severity": self.severity,
            "source": self.source,
            "commit_sha": self.commit_sha,
            "created_at": self.created_at,
            "hit_count": self.hit_count,
        }


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def normalize_severity(value: str) -> str:
    v = (value or "high").strip().lower()
    return v if v in SEVERITY_ORDER else "high"


def scar_fingerprint(file_path: str, symbol: str, error_summary: str, commit_sha: str = "") -> str:
    norm = "|".join(
        [file_path.replace("\\", "/").strip().lower(), symbol.strip().lower(),
         re.sub(r"\s+", " ", error_summary.strip().lower()), commit_sha.strip().lower()]
    )
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


class AetherDB:
    """Thread-safe SQLite store (WAL mode) for the Aether brain."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30.0, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        if str(self.path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self.fts_enabled = False
        self._init_schema()

    # ------------------------------------------------------------------ lifecycle
    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            try:
                self._conn.executescript(FTS_SCHEMA)
                self.fts_enabled = True
            except sqlite3.OperationalError:
                self.fts_enabled = False
            # Every schema change is additive (CREATE ... IF NOT EXISTS), so older brains upgrade in place.
            for key, value in (("schema_version", SCHEMA_VERSION), ("schema_revision", SCHEMA_REVISION)):
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return None if row is None else row[0]

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        val = self.scalar("SELECT value FROM meta WHERE key = ?", (key,))
        return default if val is None else str(val)

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def bump_index_version(self) -> int:
        with self._lock:
            current = int(self.get_meta("index_version", "0") or 0) + 1
            self.set_meta("index_version", str(current))
            return current

    def index_version(self) -> int:
        return int(self.get_meta("index_version", "0") or 0)

    # ------------------------------------------------------------------ repos
    def upsert_repo(self, name: str, root_path: Path | str, is_primary: bool = False) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO repos(name, root_path, is_primary) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET root_path = excluded.root_path, "
                "is_primary = MAX(repos.is_primary, excluded.is_primary)",
                (name, str(Path(root_path).resolve()), 1 if is_primary else 0),
            )
            return int(self.scalar("SELECT id FROM repos WHERE name = ?", (name,)))

    def mark_repo_indexed(self, repo_id: int) -> None:
        self.execute("UPDATE repos SET indexed_at = ? WHERE id = ?", (time.time(), repo_id))

    def list_repos(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT r.*, (SELECT COUNT(*) FROM files f WHERE f.repo_id = r.id) AS file_count "
            "FROM repos r ORDER BY r.is_primary DESC, r.name"
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ files
    def get_file_hashes(self, repo_id: int) -> dict[str, tuple[int, str]]:
        rows = self.query("SELECT id, path, sha1 FROM files WHERE repo_id = ?", (repo_id,))
        return {r["path"]: (int(r["id"]), r["sha1"]) for r in rows}

    def delete_file(self, file_id: int) -> None:
        with self._lock:
            if self.fts_enabled:
                self._conn.execute(
                    "DELETE FROM nodes_fts WHERE node_id IN (SELECT id FROM nodes WHERE file_id = ?)",
                    (file_id,),
                )
            self._conn.execute("UPDATE edges SET dst_file_id = NULL WHERE dst_file_id = ?", (file_id,))
            self._conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    def replace_file(
        self,
        repo_id: int,
        path: str,
        language: str,
        sha1: str,
        size_bytes: int,
        line_count: int,
        token_estimate: int,
        nodes: Iterable[dict[str, Any]],
        edges: Iterable[dict[str, Any]],
    ) -> int:
        """Atomically (re)write a file with its nodes and outgoing edges.

        ``nodes`` dicts: kind, name, qualname, signature, docstring, body, start_line, end_line, metadata.
        ``edges`` dicts: kind, dst_name, src_qualname (optional), metadata.
        """
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM files WHERE repo_id = ? AND path = ?", (repo_id, path)
            ).fetchone()
            if existing is not None:
                fid_old = int(existing["id"])
                if self.fts_enabled:
                    conn.execute(
                        "DELETE FROM nodes_fts WHERE node_id IN (SELECT id FROM nodes WHERE file_id = ?)",
                        (fid_old,),
                    )
                conn.execute("DELETE FROM edges WHERE src_file_id = ?", (fid_old,))
                conn.execute("DELETE FROM nodes WHERE file_id = ?", (fid_old,))
                conn.execute(
                    "UPDATE files SET language=?, sha1=?, size_bytes=?, line_count=?, "
                    "token_estimate=?, indexed_at=? WHERE id=?",
                    (language, sha1, size_bytes, line_count, token_estimate, time.time(), fid_old),
                )
                file_id = fid_old
            else:
                cur = conn.execute(
                    "INSERT INTO files(repo_id, path, language, sha1, size_bytes, line_count, "
                    "token_estimate, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (repo_id, path, language, sha1, size_bytes, line_count, token_estimate, time.time()),
                )
                file_id = int(cur.lastrowid or 0)

            qual_to_id: dict[str, int] = {}
            for n in nodes:
                cur = conn.execute(
                    "INSERT INTO nodes(file_id, kind, name, qualname, signature, docstring, body, "
                    "start_line, end_line, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        file_id,
                        n["kind"],
                        n["name"],
                        n.get("qualname") or n["name"],
                        n.get("signature", ""),
                        n.get("docstring", ""),
                        n.get("body", ""),
                        int(n.get("start_line", 1)),
                        int(n.get("end_line", n.get("start_line", 1))),
                        json.dumps(n.get("metadata", {}), ensure_ascii=False),
                    ),
                )
                node_id = int(cur.lastrowid or 0)
                qual_to_id.setdefault(n.get("qualname") or n["name"], node_id)
                if self.fts_enabled:
                    conn.execute(
                        "INSERT INTO nodes_fts(name, qualname, signature, docstring, body, path, node_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            expand_for_fts(n["name"]),
                            expand_for_fts(n.get("qualname") or n["name"]),
                            n.get("signature", ""),
                            n.get("docstring", ""),
                            expand_for_fts(n.get("body", "")[:6000]),
                            expand_for_fts(path),
                            node_id,
                        ),
                    )
            for e in edges:
                src_q = e.get("src_qualname")
                conn.execute(
                    "INSERT INTO edges(kind, src_file_id, src_node_id, dst_name, metadata) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        e["kind"],
                        file_id,
                        qual_to_id.get(src_q) if src_q else None,
                        e["dst_name"],
                        json.dumps(e.get("metadata", {}), ensure_ascii=False),
                    ),
                )
            return file_id

    def file_by_path(self, path: str, repo_name: str | None = None) -> FileRecord | None:
        norm = normalize_rel_path(path)
        params: list[Any] = [norm]
        sql = (
            "SELECT f.*, r.name AS repo_name FROM files f JOIN repos r ON r.id = f.repo_id "
            "WHERE f.path = ?"
        )
        if repo_name:
            sql += " AND r.name = ?"
            params.append(repo_name)
        sql += " ORDER BY r.is_primary DESC LIMIT 1"
        rows = self.query(sql, params)
        if not rows:
            rows = self.query(
                "SELECT f.*, r.name AS repo_name FROM files f JOIN repos r ON r.id = f.repo_id "
                "WHERE f.path LIKE ? ORDER BY r.is_primary DESC, LENGTH(f.path) LIMIT 1",
                (f"%{norm}",),
            )
        return self._file_from_row(rows[0]) if rows else None

    def file_by_id(self, file_id: int) -> FileRecord | None:
        rows = self.query(
            "SELECT f.*, r.name AS repo_name FROM files f JOIN repos r ON r.id = f.repo_id WHERE f.id = ?",
            (file_id,),
        )
        return self._file_from_row(rows[0]) if rows else None

    def list_files(self, repo_name: str | None = None) -> list[FileRecord]:
        sql = "SELECT f.*, r.name AS repo_name FROM files f JOIN repos r ON r.id = f.repo_id"
        params: tuple[Any, ...] = ()
        if repo_name:
            sql += " WHERE r.name = ?"
            params = (repo_name,)
        sql += " ORDER BY r.name, f.path"
        return [self._file_from_row(r) for r in self.query(sql, params)]

    @staticmethod
    def _file_from_row(row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            id=int(row["id"]),
            repo_id=int(row["repo_id"]),
            repo_name=row["repo_name"],
            path=row["path"],
            language=row["language"],
            sha1=row["sha1"],
            size_bytes=int(row["size_bytes"]),
            line_count=int(row["line_count"]),
            token_estimate=int(row["token_estimate"]),
        )

    # ------------------------------------------------------------------ nodes
    _NODE_SELECT = (
        "SELECT n.*, f.path AS path, r.name AS repo_name FROM nodes n "
        "JOIN files f ON f.id = n.file_id JOIN repos r ON r.id = f.repo_id"
    )

    @staticmethod
    def _node_from_row(row: sqlite3.Row) -> NodeRecord:
        try:
            meta = json.loads(row["metadata"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        return NodeRecord(
            id=int(row["id"]),
            file_id=int(row["file_id"]),
            repo_name=row["repo_name"],
            path=row["path"],
            kind=row["kind"],
            name=row["name"],
            qualname=row["qualname"],
            signature=row["signature"],
            docstring=row["docstring"],
            body=row["body"],
            start_line=int(row["start_line"]),
            end_line=int(row["end_line"]),
            metadata=meta,
        )

    def nodes_for_file(self, file_id: int) -> list[NodeRecord]:
        rows = self.query(self._NODE_SELECT + " WHERE n.file_id = ? ORDER BY n.start_line", (file_id,))
        return [self._node_from_row(r) for r in rows]

    def nodes_by_ids(self, ids: Sequence[int]) -> list[NodeRecord]:
        if not ids:
            return []
        out: list[NodeRecord] = []
        for i in range(0, len(ids), 500):
            chunk = list(ids[i : i + 500])
            marks = ",".join("?" * len(chunk))
            rows = self.query(self._NODE_SELECT + f" WHERE n.id IN ({marks})", chunk)
            out.extend(self._node_from_row(r) for r in rows)
        order = {nid: i for i, nid in enumerate(ids)}
        out.sort(key=lambda n: order.get(n.id, 0))
        return out

    def nodes_by_kind(self, kind: str) -> list[NodeRecord]:
        rows = self.query(self._NODE_SELECT + " WHERE n.kind = ? ORDER BY f.path", (kind,))
        return [self._node_from_row(r) for r in rows]

    def all_nodes_for_search(self) -> list[tuple[int, str]]:
        """Return (node_id, document_text) for building in-memory vector indexes."""
        rows = self.query(
            "SELECT n.id, n.name, n.qualname, n.signature, n.docstring, n.body, f.path "
            "FROM nodes n JOIN files f ON f.id = n.file_id"
        )
        docs: list[tuple[int, str]] = []
        for r in rows:
            text = " ".join(
                [
                    expand_for_fts(r["qualname"]),
                    r["signature"] or "",
                    r["docstring"] or "",
                    expand_for_fts((r["body"] or "")[:3000]),
                    expand_for_fts(r["path"]),
                ]
            )
            docs.append((int(r["id"]), text))
        return docs

    def fts_search(self, query: str, limit: int = 50) -> list[tuple[int, float]]:
        """BM25 search; returns (node_id, score) with higher = better."""
        terms = [t for t in re.findall(r"[A-Za-z0-9_]+", query) if len(t) > 1]
        expanded: list[str] = []
        for t in terms:
            expanded.append(t.lower())
            expanded.extend(split_identifier(t))
        uniq = list(dict.fromkeys(w for w in expanded if len(w) > 1))
        if not uniq:
            return []
        if self.fts_enabled:
            match = " OR ".join(f'"{w}"*' for w in uniq[:24])
            try:
                rows = self.query(
                    "SELECT node_id, bm25(nodes_fts, 8.0, 6.0, 3.0, 2.0, 1.0, 2.0) AS score "
                    "FROM nodes_fts WHERE nodes_fts MATCH ? ORDER BY score LIMIT ?",
                    (match, limit),
                )
                return [(int(r["node_id"]), -float(r["score"])) for r in rows]
            except sqlite3.OperationalError:
                pass
        # Fallback LIKE search when FTS5 is unavailable.
        scores: dict[int, float] = {}
        for w in uniq[:12]:
            rows = self.query(
                "SELECT n.id FROM nodes n JOIN files f ON f.id = n.file_id WHERE n.name LIKE ? "
                "OR n.qualname LIKE ? OR n.docstring LIKE ? OR n.body LIKE ? OR f.path LIKE ? LIMIT ?",
                (f"%{w}%",) * 5 + (limit * 2,),
            )
            for r in rows:
                scores[int(r["id"])] = scores.get(int(r["id"]), 0.0) + 1.0
        return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]

    # ------------------------------------------------------------------ edges
    def resolve_edge(self, edge_id: int, dst_file_id: int | None, dst_node_id: int | None) -> None:
        self.execute(
            "UPDATE edges SET dst_file_id = ?, dst_node_id = ? WHERE id = ?",
            (dst_file_id, dst_node_id, edge_id),
        )

    def unresolved_edges(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT e.*, f.path AS src_path, f.language AS src_language, r.name AS repo_name, "
            "r.id AS repo_id FROM edges e JOIN files f ON f.id = e.src_file_id "
            "JOIN repos r ON r.id = f.repo_id WHERE e.dst_file_id IS NULL"
        )

    def file_dependents(self, file_id: int) -> list[sqlite3.Row]:
        """Files with an edge pointing at ``file_id`` (who depends on me)."""
        return self.query(
            "SELECT DISTINCT e.kind, e.dst_name, f.id AS file_id, f.path, r.name AS repo_name "
            "FROM edges e JOIN files f ON f.id = e.src_file_id JOIN repos r ON r.id = f.repo_id "
            "WHERE e.dst_file_id = ? AND e.src_file_id != ?",
            (file_id, file_id),
        )

    def file_dependencies(self, file_id: int) -> list[sqlite3.Row]:
        """Edges leaving ``file_id`` (what I depend on)."""
        return self.query(
            "SELECT e.kind, e.dst_name, e.dst_file_id, f.path AS dst_path, r.name AS dst_repo "
            "FROM edges e LEFT JOIN files f ON f.id = e.dst_file_id "
            "LEFT JOIN repos r ON r.id = f.repo_id WHERE e.src_file_id = ?",
            (file_id,),
        )

    def file_graph(self, limit_files: int = 1500) -> dict[str, Any]:
        files = self.query(
            "SELECT f.id, f.path, f.language, f.line_count, f.token_estimate, r.name AS repo_name, "
            "(SELECT COUNT(*) FROM nodes n WHERE n.file_id = f.id) AS node_count, "
            "(SELECT COUNT(*) FROM scars s WHERE s.file_path = f.path) AS scar_count, "
            "(SELECT COUNT(*) FROM nodes n WHERE n.file_id = f.id AND n.kind = 'route') AS route_count "
            "FROM files f JOIN repos r ON r.id = f.repo_id ORDER BY node_count DESC LIMIT ?",
            (limit_files,),
        )
        ids = {int(f["id"]) for f in files}
        edges = self.query(
            "SELECT e.src_file_id AS src, e.dst_file_id AS dst, e.kind, COUNT(*) AS weight "
            "FROM edges e WHERE e.dst_file_id IS NOT NULL AND e.src_file_id != e.dst_file_id "
            "GROUP BY e.src_file_id, e.dst_file_id, e.kind"
        )
        return {
            "nodes": [dict(f) for f in files],
            "edges": [dict(e) for e in edges if int(e["src"]) in ids and int(e["dst"]) in ids],
        }

    # ------------------------------------------------------------------ scars
    def add_scar(
        self,
        file_path: str,
        error_summary: str,
        fix_applied: str = "",
        severity: str = "high",
        root_cause: str = "",
        symbol: str = "",
        source: str = "manual",
        commit_sha: str = "",
        repo_name: str = "",
        created_at: float | None = None,
    ) -> tuple[int, bool]:
        """Insert a scar; returns (scar_id, created). Duplicates (same fingerprint) are merged."""
        fp = file_path.replace("\\", "/")
        fingerprint = scar_fingerprint(fp, symbol, error_summary, commit_sha)
        with self._lock:
            existing = self.scalar("SELECT id FROM scars WHERE fingerprint = ?", (fingerprint,))
            if existing is not None:
                if fix_applied:
                    self.execute(
                        "UPDATE scars SET fix_applied = ?, severity = ? WHERE id = ?",
                        (fix_applied, normalize_severity(severity), existing),
                    )
                return int(existing), False
            cur = self.execute(
                "INSERT INTO scars(fingerprint, repo_name, file_path, symbol, error_summary, root_cause, "
                "fix_applied, severity, source, commit_sha, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fingerprint,
                    repo_name,
                    fp,
                    symbol,
                    error_summary.strip(),
                    root_cause.strip(),
                    fix_applied.strip(),
                    normalize_severity(severity),
                    source,
                    commit_sha,
                    created_at if created_at is not None else time.time(),
                ),
            )
            return int(cur.lastrowid or 0), True

    @staticmethod
    def _scar_from_row(row: sqlite3.Row) -> ScarRecord:
        return ScarRecord(
            id=int(row["id"]),
            fingerprint=row["fingerprint"],
            repo_name=row["repo_name"],
            file_path=row["file_path"],
            symbol=row["symbol"],
            error_summary=row["error_summary"],
            root_cause=row["root_cause"],
            fix_applied=row["fix_applied"],
            severity=row["severity"],
            source=row["source"],
            commit_sha=row["commit_sha"],
            created_at=float(row["created_at"]),
            hit_count=int(row["hit_count"]),
        )

    def scars_for_path(self, file_path: str, record_hit: bool = True) -> list[ScarRecord]:
        fp = normalize_rel_path(file_path)
        rows = self.query(
            "SELECT * FROM scars WHERE file_path = ? OR file_path LIKE ? OR ? LIKE '%' || file_path",
            (fp, f"%/{fp}", fp),
        )
        scars = [self._scar_from_row(r) for r in rows]
        scars.sort(key=lambda s: (SEVERITY_ORDER.get(s.severity, 9), -s.created_at))
        if record_hit and scars:
            now = time.time()
            with self._lock:
                for s in scars:
                    self._conn.execute(
                        "UPDATE scars SET hit_count = hit_count + 1, last_hit_at = ? WHERE id = ?",
                        (now, s.id),
                    )
        return scars

    def list_scars(self, limit: int = 500) -> list[ScarRecord]:
        rows = self.query("SELECT * FROM scars ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))
        return [self._scar_from_row(r) for r in rows]

    def delete_scar(self, scar_id: int) -> bool:
        cur = self.execute("DELETE FROM scars WHERE id = ?", (scar_id,))
        return cur.rowcount > 0

    # ------------------------------------------------------------------ decisions
    def add_decision(self, title: str, body: str, tags: Sequence[str] = (), created_at: float | None = None) -> bool:
        cur = self.execute(
            "INSERT OR IGNORE INTO decisions(title, body, tags, created_at) VALUES (?, ?, ?, ?)",
            (title.strip(), body.strip(), json.dumps(list(tags)), created_at or time.time()),
        )
        return cur.rowcount > 0

    def list_decisions(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM decisions ORDER BY created_at DESC")
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d.get("tags") or "[]")
            out.append(d)
        return out

    # ------------------------------------------------------------------ telemetry
    def record_telemetry(
        self,
        kind: str,
        raw_tokens: int = 0,
        compressed_tokens: int = 0,
        saved_usd: float = 0.0,
        latency_ms: float = 0.0,
        payload: dict[str, Any] | None = None,
    ) -> None:
        saved = max(0, raw_tokens - compressed_tokens)
        self.execute(
            "INSERT INTO telemetry(ts, kind, raw_tokens, compressed_tokens, saved_tokens, saved_usd, "
            "latency_ms, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), kind, raw_tokens, compressed_tokens, saved, saved_usd, latency_ms,
             json.dumps(payload or {}, ensure_ascii=False)),
        )

    def telemetry_totals(self) -> dict[str, Any]:
        row = self.query(
            "SELECT COUNT(*) AS queries, COALESCE(SUM(raw_tokens),0) AS raw, "
            "COALESCE(SUM(compressed_tokens),0) AS compressed, COALESCE(SUM(saved_tokens),0) AS saved, "
            "COALESCE(SUM(saved_usd),0) AS usd, COALESCE(AVG(latency_ms),0) AS latency "
            "FROM telemetry WHERE kind = 'context_query'"
        )[0]
        return {
            "queries": int(row["queries"]),
            "raw_tokens": int(row["raw"]),
            "compressed_tokens": int(row["compressed"]),
            "saved_tokens": int(row["saved"]),
            "saved_usd": round(float(row["usd"]), 6),
            "avg_latency_ms": round(float(row["latency"]), 2),
        }

    def telemetry_series(self, limit: int = 60) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT ts, saved_tokens, saved_usd, latency_ms FROM telemetry "
            "WHERE kind = 'context_query' ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in reversed(rows)]

    def telemetry_latency_percentiles(self) -> dict[str, float]:
        """p50/p95 latency of context queries (exact, computed over every recorded query)."""
        rows = self.query("SELECT latency_ms FROM telemetry WHERE kind = 'context_query' ORDER BY latency_ms")
        values = [float(r["latency_ms"]) for r in rows]
        if not values:
            return {"p50": 0.0, "p95": 0.0}

        def pct(q: float) -> float:
            idx = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
            return round(values[idx], 2)

        return {"p50": pct(0.5), "p95": pct(0.95)}

    def telemetry_top_queries(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT ts, compressed_tokens, raw_tokens, saved_tokens, latency_ms, payload FROM telemetry "
            "WHERE kind = 'context_query' ORDER BY compressed_tokens DESC, id DESC LIMIT ?",
            (limit,),
        )
        out = []
        for r in rows:
            payload = json.loads(r["payload"] or "{}")
            out.append({"query": str(payload.get("query", "")), "compressed_tokens": int(r["compressed_tokens"]),
                        "raw_tokens": int(r["raw_tokens"]), "saved_tokens": int(r["saved_tokens"]),
                        "latency_ms": round(float(r["latency_ms"]), 2), "ts": float(r["ts"])})
        return out

    # ------------------------------------------------------------------ session checkpoints
    def add_checkpoint(self, session: str, goal: str, files: Sequence[str], pending: Sequence[str],
                       decisions: Sequence[str], notes: str, summary: str, tokens: int) -> int:
        cur = self.execute(
            "INSERT INTO session_checkpoints(created_at, session, goal, files, pending, decisions, notes, summary, tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), session, goal, json.dumps(list(files), ensure_ascii=False),
             json.dumps(list(pending), ensure_ascii=False), json.dumps(list(decisions), ensure_ascii=False),
             notes, summary, int(tokens)),
        )
        return int(cur.lastrowid or 0)

    @staticmethod
    def _checkpoint_row(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(r["id"]), "created_at": float(r["created_at"]), "session": r["session"], "goal": r["goal"],
            "files": json.loads(r["files"] or "[]"), "pending": json.loads(r["pending"] or "[]"),
            "decisions": json.loads(r["decisions"] or "[]"), "notes": r["notes"], "summary": r["summary"],
            "tokens": int(r["tokens"]),
        }

    def get_checkpoint(self, checkpoint_id: int | None = None) -> dict[str, Any] | None:
        if checkpoint_id is None:
            rows = self.query("SELECT * FROM session_checkpoints ORDER BY created_at DESC, id DESC LIMIT 1")
        else:
            rows = self.query("SELECT * FROM session_checkpoints WHERE id = ?", (int(checkpoint_id),))
        return self._checkpoint_row(rows[0]) if rows else None

    def list_checkpoints(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM session_checkpoints ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))
        return [self._checkpoint_row(r) for r in rows]

    # ------------------------------------------------------------------ context budget
    def record_budget(self, session: str, tool: str, tokens: int, detail: dict[str, Any] | None = None) -> None:
        self.execute(
            "INSERT INTO budget_usage(ts, session, tool, tokens, detail) VALUES (?, ?, ?, ?, ?)",
            (time.time(), session, tool, int(tokens), json.dumps(detail or {}, ensure_ascii=False, default=str)),
        )

    def budget_session_total(self, session: str) -> int:
        return int(self.scalar("SELECT COALESCE(SUM(tokens), 0) FROM budget_usage WHERE session = ?", (session,)) or 0)

    def budget_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT session, COUNT(*) AS calls, SUM(tokens) AS tokens, MIN(ts) AS started, MAX(ts) AS last "
            "FROM budget_usage GROUP BY session ORDER BY last DESC LIMIT ?",
            (limit,),
        )
        return [{"session": r["session"], "calls": int(r["calls"]), "tokens": int(r["tokens"] or 0),
                 "started": float(r["started"]), "last": float(r["last"])} for r in rows]

    def budget_by_tool(self, session: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT tool, COUNT(*) AS calls, SUM(tokens) AS tokens, MAX(tokens) AS peak FROM budget_usage"
        params: tuple[Any, ...] = ()
        if session:
            sql += " WHERE session = ?"
            params = (session,)
        sql += " GROUP BY tool ORDER BY tokens DESC"
        return [{"tool": r["tool"], "calls": int(r["calls"]), "tokens": int(r["tokens"] or 0), "peak": int(r["peak"] or 0)}
                for r in self.query(sql, params)]

    def budget_details(self, session: str | None = None, limit: int = 2000) -> list[dict[str, Any]]:
        sql = "SELECT ts, session, tool, tokens, detail FROM budget_usage"
        params: list[Any] = []
        if session:
            sql += " WHERE session = ?"
            params.append(session)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [{"ts": float(r["ts"]), "session": r["session"], "tool": r["tool"], "tokens": int(r["tokens"]),
                 "detail": json.loads(r["detail"] or "{}")} for r in self.query(sql, params)]

    # ------------------------------------------------------------------ events
    def insert_event(self, type_: str, origin: str, nodes: Sequence[str], payload: dict[str, Any], ts: float) -> int:
        cur = self.execute(
            "INSERT INTO events(ts, type, origin, nodes, payload) VALUES (?, ?, ?, ?, ?)",
            (ts, type_, origin, json.dumps(list(nodes)), json.dumps(payload, ensure_ascii=False, default=str)),
        )
        return int(cur.lastrowid or 0)

    def events_since(self, last_id: int, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (last_id, limit))
        return [self._event_row(r) for r in rows]

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [self._event_row(r) for r in reversed(rows)]

    def max_event_id(self) -> int:
        return int(self.scalar("SELECT COALESCE(MAX(id), 0) FROM events") or 0)

    def prune_events(self, keep: int = 5000) -> None:
        self.execute("DELETE FROM events WHERE id <= (SELECT COALESCE(MAX(id),0) - ? FROM events)", (keep,))

    @staticmethod
    def _event_row(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(r["id"]),
            "ts": float(r["ts"]),
            "type": r["type"],
            "origin": r["origin"],
            "nodes": json.loads(r["nodes"] or "[]"),
            "payload": json.loads(r["payload"] or "{}"),
        }

    # ------------------------------------------------------------------ security findings
    def add_security_finding(self, kind: str, rule: str, severity: str, file_path: str = "", line: int = 0, detail: str = "") -> None:
        self.execute(
            "INSERT INTO security_findings(ts, kind, rule, severity, file_path, line, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), kind, rule, severity, file_path, line, detail),
        )

    def security_counts(self) -> dict[str, int]:
        rows = self.query("SELECT kind, COUNT(*) AS c FROM security_findings GROUP BY kind")
        return {r["kind"]: int(r["c"]) for r in rows}

    # ------------------------------------------------------------------ snapshots
    def add_snapshot(self, snap_id: str, label: str, created_at: float, file_count: int, total_bytes: int, manifest_path: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO snapshots(id, label, created_at, file_count, total_bytes, manifest_path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (snap_id, label, created_at, file_count, total_bytes, manifest_path),
        )

    def list_snapshots(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.query("SELECT * FROM snapshots ORDER BY created_at DESC")]

    # ------------------------------------------------------------------ swarm
    def save_swarm_run(self, run_id: str, description: str, status: str, data: dict[str, Any]) -> None:
        now = time.time()
        self.execute(
            "INSERT INTO swarm_runs(id, description, status, created_at, updated_at, data) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at, "
            "data = excluded.data",
            (run_id, description, status, now, now, json.dumps(data, ensure_ascii=False, default=str)),
        )

    def get_swarm_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM swarm_runs WHERE id = ?", (run_id,))
        if not rows:
            return None
        d = dict(rows[0])
        d["data"] = json.loads(d["data"] or "{}")
        return d

    def list_swarm_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query("SELECT id, description, status, created_at, updated_at FROM swarm_runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ stats
    def counts(self) -> dict[str, int]:
        return {
            "repos": int(self.scalar("SELECT COUNT(*) FROM repos") or 0),
            "files": int(self.scalar("SELECT COUNT(*) FROM files") or 0),
            "nodes": int(self.scalar("SELECT COUNT(*) FROM nodes") or 0),
            "edges": int(self.scalar("SELECT COUNT(*) FROM edges") or 0),
            "resolved_edges": int(self.scalar("SELECT COUNT(*) FROM edges WHERE dst_file_id IS NOT NULL") or 0),
            "scars": int(self.scalar("SELECT COUNT(*) FROM scars") or 0),
            "decisions": int(self.scalar("SELECT COUNT(*) FROM decisions") or 0),
            "snapshots": int(self.scalar("SELECT COUNT(*) FROM snapshots") or 0),
            "routes": int(self.scalar("SELECT COUNT(*) FROM nodes WHERE kind = 'route'") or 0),
            "checkpoints": int(self.scalar("SELECT COUNT(*) FROM session_checkpoints") or 0),
        }
