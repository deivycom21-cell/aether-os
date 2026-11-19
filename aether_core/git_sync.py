"""Git-synced team brain (``.aether/team_brain.json.gz``) and time-travel snapshots.

* The team brain contains only non-sensitive knowledge: structural graph (paths, symbols, signatures),
  scars and architecture decisions. Every string goes through the secret firewall before export and
  file bodies are never included.
* Snapshots use a content-addressed object store (``.aether/snapshots/_objects``) so repeated
  snapshots only copy changed files, and every rollback first takes an automatic safety snapshot.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aether_core import __version__
from aether_core.security import SecretVault

if TYPE_CHECKING:
    from aether_core.database import AetherDB
    from aether_core.event_bus import EventBus

TEAM_BRAIN_FILENAME = "team_brain.json.gz"
BRAIN_FORMAT = "aether-team-brain/1"

SNAPSHOT_EXCLUDE_DIRS = {
    ".git", ".aether", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".next", "target",
}
SNAPSHOT_MAX_FILE_BYTES = 20 * 1024 * 1024


# ---------------------------------------------------------------------------- team brain


def export_team_brain(db: AetherDB, out_path: Path, bus: EventBus | None = None) -> dict[str, Any]:
    vault = SecretVault()
    masked_total = 0

    def clean(value: Any) -> Any:
        nonlocal masked_total
        out, findings = vault.mask_obj(value)
        masked_total += len(findings)
        return out

    repos = [{"name": r["name"], "is_primary": bool(r["is_primary"])} for r in db.list_repos()]
    files_out: list[dict[str, Any]] = []
    file_index: dict[int, int] = {}
    for f in db.list_files():
        file_index[f.id] = len(files_out)
        nodes = [
            {
                "kind": n.kind,
                "name": n.name,
                "qualname": n.qualname,
                "signature": clean(n.signature),
                "doc": clean(n.docstring[:500]),
                "lines": [n.start_line, n.end_line],
            }
            for n in db.nodes_for_file(f.id)
        ]
        files_out.append(
            {"repo": f.repo_name, "path": f.path, "language": f.language, "sha1": f.sha1,
             "lines": f.line_count, "tokens": f.token_estimate, "nodes": nodes}
        )
    edges = []
    for row in db.query("SELECT src_file_id, dst_file_id, kind, COUNT(*) AS w FROM edges WHERE dst_file_id IS NOT NULL GROUP BY 1,2,3"):
        s, d = file_index.get(int(row["src_file_id"])), file_index.get(int(row["dst_file_id"]))
        if s is not None and d is not None:
            edges.append([s, d, row["kind"], int(row["w"])])
    scars = [clean({k: v for k, v in s.to_dict().items() if k not in {"id", "hit_count"}}) for s in db.list_scars(10_000)]
    decisions = [clean({"title": d["title"], "body": d["body"], "tags": d["tags"], "created_at": d["created_at"]})
                 for d in db.list_decisions()]
    doc = {
        "format": BRAIN_FORMAT,
        "aether_version": __version__,
        "repos": repos,
        "files": files_out,
        "edges": edges,
        "scars": scars,
        "decisions": decisions,
    }
    # No wall-clock timestamp in the payload: identical knowledge must produce identical bytes.
    doc["content_sha256"] = hashlib.sha256(
        json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    tmp = out_path.with_suffix(".tmp")
    # mtime=0 keeps the gzip byte-identical for identical content -> clean git diffs.
    with open(tmp, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
        gz.write(payload)
    os.replace(tmp, out_path)
    stats = {
        "path": str(out_path),
        "files": len(files_out),
        "edges": len(edges),
        "scars": len(scars),
        "decisions": len(decisions),
        "secrets_masked": masked_total,
        "bytes": out_path.stat().st_size,
    }
    if bus:
        bus.publish("BRAIN_SYNC", {"direction": "export", **stats})
    return stats


def load_team_brain(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rb") as gz:
        doc = json.loads(gz.read().decode("utf-8"))
    if doc.get("format") != BRAIN_FORMAT:
        raise ValueError(f"Unsupported team brain format: {doc.get('format')!r}")
    return doc


def import_team_brain(db: AetherDB, path: Path, bus: EventBus | None = None) -> dict[str, Any]:
    """Merge scars and decisions from a teammate's brain (graph is rebuilt locally by indexing)."""
    doc = load_team_brain(path)
    new_scars = 0
    for s in doc.get("scars", []):
        _, created = db.add_scar(
            file_path=s.get("file_path", ""),
            error_summary=s.get("error_summary", ""),
            fix_applied=s.get("fix_applied", ""),
            severity=s.get("severity", "high"),
            root_cause=s.get("root_cause", ""),
            symbol=s.get("symbol", ""),
            source=s.get("source", "team"),
            commit_sha=s.get("commit_sha", ""),
            repo_name=s.get("repo", ""),
            created_at=s.get("created_at"),
        )
        new_scars += int(created)
    new_decisions = 0
    for d in doc.get("decisions", []):
        new_decisions += int(db.add_decision(d["title"], d["body"], d.get("tags", []), d.get("created_at")))
    local_paths = {f.path for f in db.list_files()}
    remote_paths = {f["path"] for f in doc.get("files", [])}
    stats = {
        "path": str(path),
        "content_sha256": doc.get("content_sha256"),
        "scars_imported": new_scars,
        "decisions_imported": new_decisions,
        "remote_files": len(remote_paths),
        "files_only_remote": len(remote_paths - local_paths),
        "files_only_local": len(local_paths - remote_paths) if local_paths else 0,
    }
    if bus:
        bus.publish("BRAIN_SYNC", {"direction": "import", **stats})
    return stats


# ---------------------------------------------------------------------------- snapshots


@dataclass(slots=True)
class SnapshotInfo:
    id: str
    label: str
    created_at: float
    file_count: int
    total_bytes: int
    new_objects: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "created_at": self.created_at,
            "created_at_iso": datetime.fromtimestamp(self.created_at, timezone.utc).isoformat(),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "new_objects": self.new_objects,
        }


def _git_tracked_files(root: Path) -> list[str] | None:
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=60, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [p for p in out.decode("utf-8", errors="replace").split("\0") if p]


def _walk_files(root: Path) -> list[str]:
    out: list[str] = []
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(cur.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_symlink():
                continue
            if e.is_dir():
                if e.name not in SNAPSHOT_EXCLUDE_DIRS:
                    stack.append(e)
            else:
                out.append(e.relative_to(root).as_posix())
    return out


class SnapshotManager:
    def __init__(self, root: Path, aether_dir: Path, db: AetherDB, bus: EventBus | None = None) -> None:
        self.root = root.resolve()
        self.base = aether_dir / "snapshots"
        self.objects = self.base / "_objects"
        self.db = db
        self.bus = bus

    def tracked_files(self) -> list[str]:
        files = _git_tracked_files(self.root)
        if files is None:
            files = _walk_files(self.root)
        result = []
        for rel in files:
            first = rel.split("/", 1)[0]
            if first in SNAPSHOT_EXCLUDE_DIRS:
                continue
            p = self.root / rel
            if p.is_file() and not p.is_symlink():
                result.append(rel)
        return sorted(set(result))

    def _store_object(self, data: bytes) -> tuple[str, bool]:
        digest = hashlib.sha256(data).hexdigest()
        target = self.objects / digest[:2] / digest
        if target.exists():
            return digest, False
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return digest, True

    def create(self, label: str = "manual") -> SnapshotInfo:
        created = time.time()
        snap_id = datetime.fromtimestamp(created).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        entries: list[dict[str, Any]] = []
        total = 0
        new_objects = 0
        for rel in self.tracked_files():
            p = self.root / rel
            try:
                st = p.stat()
                if st.st_size > SNAPSHOT_MAX_FILE_BYTES:
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            digest, is_new = self._store_object(data)
            new_objects += int(is_new)
            total += len(data)
            entries.append({"path": rel, "sha256": digest, "size": len(data), "mode": st.st_mode & 0o777})
        snap_dir = self.base / snap_id
        snap_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "id": snap_id,
            "label": label,
            "created_at": created,
            "root": str(self.root),
            "files": entries,
        }
        manifest_path = snap_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
        self.db.add_snapshot(snap_id, label, created, len(entries), total, str(manifest_path))
        info = SnapshotInfo(snap_id, label, created, len(entries), total, new_objects)
        if self.bus:
            self.bus.publish("SNAPSHOT_CREATED", info.to_dict(), [])
        return info

    def load_manifest(self, snap_id: str) -> dict[str, Any]:
        if not snap_id or any(c in snap_id for c in "/\\") or snap_id.startswith("."):
            raise ValueError(f"Invalid snapshot id: {snap_id!r}")
        path = self.base / snap_id / "manifest.json"
        if not path.is_file():
            raise FileNotFoundError(f"Snapshot not found: {snap_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        out = []
        for row in self.db.list_snapshots():
            if (self.base / row["id"] / "manifest.json").is_file():
                row["created_at_iso"] = datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat()
                out.append(row)
        return out

    def diff(self, snap_id: str) -> dict[str, list[str]]:
        manifest = self.load_manifest(snap_id)
        snap = {e["path"]: e["sha256"] for e in manifest["files"]}
        current = set(self.tracked_files())
        modified, removed = [], []
        for rel, digest in snap.items():
            p = self.root / rel
            if not p.is_file():
                removed.append(rel)
                continue
            try:
                if hashlib.sha256(p.read_bytes()).hexdigest() != digest:
                    modified.append(rel)
            except OSError:
                modified.append(rel)
        added = sorted(current - set(snap))
        return {"modified": sorted(modified), "removed": sorted(removed), "added": added}

    def rollback(self, snap_id: str, prune_new: bool = True, safety_snapshot: bool = True) -> dict[str, Any]:
        manifest = self.load_manifest(snap_id)
        changes = self.diff(snap_id)
        safety: SnapshotInfo | None = None
        if safety_snapshot and any(changes.values()):
            safety = self.create(f"auto: before rollback to {snap_id}")
        restored = 0
        for entry in manifest["files"]:
            rel = entry["path"]
            if rel not in changes["modified"] and rel not in changes["removed"]:
                continue
            target = (self.root / rel).resolve()
            if self.root not in target.parents:
                continue  # never write outside the project root
            obj = self.objects / entry["sha256"][:2] / entry["sha256"]
            if not obj.is_file():
                raise FileNotFoundError(f"Snapshot object missing for {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".aether-tmp")
            shutil.copyfile(obj, tmp)
            os.replace(tmp, target)
            try:
                os.chmod(target, entry.get("mode", 0o644) | 0o200)
            except OSError:
                pass
            restored += 1
        deleted = 0
        if prune_new:
            for rel in changes["added"]:
                target = (self.root / rel).resolve()
                if self.root in target.parents and target.is_file():
                    target.unlink()
                    deleted += 1
        result = {
            "snapshot_id": snap_id,
            "label": manifest.get("label", ""),
            "restored": restored,
            "deleted_new_files": deleted,
            "safety_snapshot": safety.id if safety else None,
            "changes": changes,
        }
        if self.bus:
            self.bus.publish("ROLLBACK", {k: v for k, v in result.items() if k != "changes"},
                             [f"*:{p}" for p in changes["modified"] + changes["removed"]][:50])
        return result
