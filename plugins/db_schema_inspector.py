"""DB schema inspector: indexes table/column *structure* (never data) from SQL DDL, Prisma schemas,
SQLAlchemy / SQLModel / Pydantic models and local SQLite files.

Results are stored as plugin-owned virtual records ``@aether/db_schema/<source>`` with one ``table``
(or ``schema`` for Pydantic) node per entity, so agents can ``query_smart_context("users table email")``
and get exact column definitions without opening migrations or databases.
"""

from __future__ import annotations

import ast
import hashlib
import re
import sqlite3
from pathlib import Path, PurePosixPath
from typing import Any

from aether_core.ast_indexer import DEFAULT_IGNORE_DIRS, VIRTUAL_PREFIX
from plugins.base import BaseAetherPlugin

SCHEMA_PREFIX = f"{VIRTUAL_PREFIX}db_schema/"
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3")
SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_SCAN_FILES = 400
_CREATE_TABLE = re.compile(
    r"create\s+(?:temp(?:orary)?\s+)?(?:virtual\s+)?table\s+(?:if\s+not\s+exists\s+)?([`\"\[]?[\w.]+[`\"\]]?)\s*\(",
    re.I,
)
_CONSTRAINT_WORDS = ("constraint", "primary key", "foreign key", "unique", "check", "index", "key ")
_PRISMA_MODEL = re.compile(r"^\s*(model|view)\s+(\w+)\s*\{([\s\S]*?)^\s*\}", re.M)
_PRISMA_ENUM = re.compile(r"^\s*enum\s+(\w+)\s*\{([\s\S]*?)^\s*\}", re.M)
_SQLA_COLUMN_CALLS = {"Column", "mapped_column", "Field", "relationship"}


def _strip_ident(name: str) -> str:
    return name.strip("`\"[]")


def _split_top_level(body: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def parse_sql_ddl(text: str) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    clean = re.sub(r"--[^\n]*", "", text)
    clean = re.sub(r"/\*[\s\S]*?\*/", "", clean)
    for m in _CREATE_TABLE.finditer(clean):
        start = m.end()
        depth, i = 1, start
        while i < len(clean) and depth:
            if clean[i] == "(":
                depth += 1
            elif clean[i] == ")":
                depth -= 1
            i += 1
        body = clean[start : i - 1]
        columns: list[dict[str, Any]] = []
        pk_cols: set[str] = set()
        for item in _split_top_level(body):
            low = item.lower()
            if low.startswith(_CONSTRAINT_WORDS):
                pk = re.search(r"primary\s+key\s*\(([^)]*)\)", item, re.I)
                if pk:
                    pk_cols |= {_strip_ident(c.strip()) for c in pk.group(1).split(",")}
                continue
            bits = item.split(None, 1)
            col = _strip_ident(bits[0])
            rest = bits[1] if len(bits) > 1 else ""
            ctype = re.split(r"\s+(?:not|null|primary|references|default|unique|check|constraint|collate|generated)\b",
                             rest, maxsplit=1, flags=re.I)[0].strip()
            ref = re.search(r"references\s+([`\"\[]?[\w.]+[`\"\]]?)", rest, re.I)
            columns.append({
                "name": col, "type": ctype or "ANY", "primary_key": bool(re.search(r"primary\s+key", rest, re.I)),
                "nullable": not re.search(r"not\s+null", rest, re.I), "references": _strip_ident(ref.group(1)) if ref else "",
            })
        for c in columns:
            if c["name"] in pk_cols:
                c["primary_key"] = True
        tables.append({"name": _strip_ident(m.group(1)), "kind": "table", "columns": columns,
                       "line": text.count("\n", 0, m.start()) + 1})
    return tables


def parse_prisma(text: str) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for m in _PRISMA_MODEL.finditer(text):
        columns = []
        mapped = m.group(2)
        for line in m.group(3).splitlines():
            line = line.split("//", 1)[0].strip()
            if not line:
                continue
            if line.startswith("@@"):
                mm = re.search(r'@@map\("([^"]+)"\)', line)
                if mm:
                    mapped = mm.group(1)
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            name, ftype = parts[0], parts[1]
            attrs = " ".join(parts[2:])
            rel = re.search(r"@relation\([^)]*references:\s*\[([^\]]+)\]", attrs)
            columns.append({"name": name, "type": ftype.rstrip("?[]") + ("[]" if ftype.endswith("[]") else ""),
                            "primary_key": "@id" in attrs, "nullable": ftype.endswith("?"),
                            "references": rel.group(1).strip() if rel else ""})
        tables.append({"name": mapped, "model": m.group(2), "kind": "table" if m.group(1) == "model" else "view",
                       "columns": columns, "line": text.count("\n", 0, m.start()) + 1})
    for m in _PRISMA_ENUM.finditer(text):
        values = [v.strip() for v in m.group(2).splitlines() if v.strip() and not v.strip().startswith("//")]
        tables.append({"name": m.group(1), "kind": "enum", "columns": [{"name": v, "type": "enum value",
                       "primary_key": False, "nullable": False, "references": ""} for v in values],
                       "line": text.count("\n", 0, m.start()) + 1})
    return tables


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def parse_python_models(text: str) -> list[dict[str, Any]]:
    """SQLAlchemy (declarative / 2.0 Mapped), SQLModel(table=True) and Pydantic BaseModel classes."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    models: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {_call_name(b) if isinstance(b, ast.Call) else ast.unparse(b).rsplit(".", 1)[-1] for b in node.bases}
        table_kw = any(k.arg == "table" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords)
        tablename = ""
        columns: list[dict[str, Any]] = []
        uses_column = False
        for item in node.body:
            if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in item.targets):
                if isinstance(item.value, ast.Constant):
                    tablename = str(item.value.value)
            elif isinstance(item, ast.Assign) and isinstance(item.value, ast.Call) and _call_name(item.value) in {"Column", "mapped_column"}:
                uses_column = True
                for t in item.targets:
                    if isinstance(t, ast.Name):
                        columns.append(_sqla_column(t.id, item.value, None))
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and not item.target.id.startswith("_"):
                ann = ast.unparse(item.annotation)
                call = item.value if isinstance(item.value, ast.Call) else None
                if call is not None and _call_name(call) == "relationship":
                    continue
                if call is not None and _call_name(call) in {"Column", "mapped_column"}:
                    uses_column = True
                    columns.append(_sqla_column(item.target.id, call, ann))
                elif ann.startswith(("ClassVar", "typing.ClassVar")):
                    continue
                else:
                    inner = re.sub(r"^Mapped\[(.*)\]$", r"\1", ann)
                    columns.append({"name": item.target.id, "type": inner, "primary_key": False,
                                    "nullable": "Optional" in inner or "None" in inner, "references": ""})
        is_sqla = bool(tablename) or uses_column or (table_kw and "SQLModel" in bases)
        is_pydantic = bool(bases & {"BaseModel", "SQLModel", "BaseSettings"}) and not is_sqla
        if not (is_sqla or is_pydantic) or not columns:
            continue
        models.append({"name": tablename or node.name, "model": node.name, "kind": "table" if is_sqla else "schema",
                       "columns": columns, "line": node.lineno,
                       "orm": "sqlmodel" if "SQLModel" in bases else "sqlalchemy" if is_sqla else "pydantic"})
    return models


def _sqla_column(name: str, call: ast.Call, annotation: str | None) -> dict[str, Any]:
    ctype = ""
    ref = ""
    for a in call.args:
        cname = _call_name(a)
        if cname == "ForeignKey" and isinstance(a, ast.Call) and a.args and isinstance(a.args[0], ast.Constant):
            ref = str(a.args[0].value)
        elif not ctype and cname:
            ctype = ast.unparse(a)
    kw = {k.arg: k.value for k in call.keywords if k.arg}
    pk = isinstance(kw.get("primary_key"), ast.Constant) and bool(kw["primary_key"].value)  # type: ignore[union-attr]
    nullable_node = kw.get("nullable")
    if isinstance(nullable_node, ast.Constant):
        nullable = bool(nullable_node.value)
    else:
        nullable = not pk and ("Optional" in (annotation or "") or "None" in (annotation or "") or annotation is None)
    if not ctype and annotation:
        ctype = re.sub(r"^Mapped\[(.*)\]$", r"\1", annotation)
    return {"name": name, "type": ctype or "ANY", "primary_key": pk, "nullable": nullable, "references": ref}


def inspect_sqlite(path: Path) -> list[dict[str, Any]]:
    """Read only ``sqlite_master`` + ``PRAGMA table_info`` through a read-only URI. Never selects rows."""
    try:
        with path.open("rb") as fh:
            if fh.read(16) != SQLITE_MAGIC:
                return []
    except OSError:
        return []
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return []
    tables: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for name, kind in rows[:500]:
            quoted = '"' + str(name).replace('"', '""') + '"'
            try:
                cols = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
                fks = {r[3]: f"{r[2]}.{r[4]}" for r in conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchall()}
            except sqlite3.Error:
                cols, fks = [], {}
            tables.append({"name": str(name), "kind": str(kind), "line": 1, "columns": [
                {"name": c[1], "type": c[2] or "ANY", "primary_key": bool(c[5]), "nullable": not c[3],
                 "references": fks.get(c[1], "")} for c in cols]})
    except sqlite3.Error:
        return tables
    finally:
        conn.close()
    return tables


def _signature(t: dict[str, Any]) -> str:
    cols = []
    for c in t["columns"][:40]:
        flags = (" PK" if c["primary_key"] else "") + ("" if c["nullable"] else " NOT NULL") + \
                (f" -> {c['references']}" if c["references"] else "")
        cols.append(f"{c['name']} {c['type']}{flags}")
    more = f", … +{len(t['columns']) - 40}" if len(t["columns"]) > 40 else ""
    return f"{t['kind']} {t['name']}({', '.join(cols)}{more})"[:500]


class DBSchemaInspector(BaseAetherPlugin):
    name = "db_schema_inspector"
    description = "Indexes table/column structure from SQL, Prisma, SQLAlchemy/Pydantic models and local SQLite (never data)."

    def _walk(self, root: Path) -> list[Path]:
        out: list[Path] = []
        stack = [root]
        while stack and len(out) < MAX_SCAN_FILES:
            cur = stack.pop()
            try:
                entries = sorted(cur.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for e in entries:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    if e.name not in DEFAULT_IGNORE_DIRS and not e.name.endswith(".egg-info"):
                        stack.append(e)
                elif e.suffix.lower() == ".prisma" or e.suffix.lower() in SQLITE_SUFFIXES:
                    out.append(e)
        return out

    def _sources(self) -> list[tuple[str, Path, str, str]]:
        """(repo, root, rel_path, source_kind) for every schema-bearing file."""
        found: list[tuple[str, Path, str, str]] = []
        roots = dict(self.repo_roots())
        for f in self.kernel.db.list_files():
            root = roots.get(f.repo_name)
            if root is None or f.path.startswith(VIRTUAL_PREFIX):
                continue
            if f.language == "sql":
                found.append((f.repo_name, root, f.path, "sql"))
            elif f.language == "python":
                found.append((f.repo_name, root, f.path, "python"))
        for repo, root in roots.items():
            for p in self._walk(root):
                rel = p.relative_to(root).as_posix()
                found.append((repo, root, rel, "prisma" if p.suffix.lower() == ".prisma" else "sqlite"))
        return found

    def is_applicable(self) -> bool:
        return bool(self.repo_roots())

    def run(self) -> dict[str, Any]:
        db = self.kernel.db
        summary: dict[str, int] = {"sql": 0, "prisma": 0, "python": 0, "sqlite": 0}
        tables_total = 0
        kept: dict[str, set[str]] = {}
        for repo, root, rel, kind in self._sources():
            path = root / Path(*PurePosixPath(rel).parts)
            if kind == "sqlite":
                entities = inspect_sqlite(path)
                fingerprint_src = repr(entities).encode("utf-8")
            else:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if kind == "python" and not re.search(r"__tablename__|Column\(|mapped_column\(|BaseModel|SQLModel", text):
                    continue
                entities = parse_sql_ddl(text) if kind == "sql" else parse_prisma(text) if kind == "prisma" else parse_python_models(text)
                fingerprint_src = text.encode("utf-8")
            if not entities:
                continue
            vrel = f"{SCHEMA_PREFIX}{rel}"
            kept.setdefault(repo, set()).add(vrel)
            nodes: list[dict[str, Any]] = [{
                "kind": "module", "name": PurePosixPath(rel).name, "qualname": vrel,
                "signature": f"{kind} schema · {len(entities)} entities",
                "docstring": f"Database structure extracted from {rel} ({kind}); no row data is ever read.",
                "body": "", "start_line": 1, "end_line": 1, "metadata": {"language": "schema", "source": rel, "source_kind": kind},
            }]
            for t in entities:
                col_lines = "\n".join(
                    f"{c['name']}: {c['type']}{' PRIMARY KEY' if c['primary_key'] else ''}"
                    f"{'' if c['nullable'] else ' NOT NULL'}{' REFERENCES ' + c['references'] if c['references'] else ''}"
                    for c in t["columns"])
                nodes.append({
                    "kind": "table" if t["kind"] in {"table", "view"} else t["kind"], "name": t["name"],
                    "qualname": f"schema:{repo}:{t['name']}" if kind != "python" else f"schema:{repo}:{t.get('model', t['name'])}",
                    "signature": _signature(t),
                    "docstring": f"{t['kind']} from {rel}" + (f" (model {t['model']}, {t.get('orm', kind)})" if t.get("model") else ""),
                    "body": col_lines[:4000], "start_line": int(t.get("line", 1)), "end_line": int(t.get("line", 1)),
                    "metadata": {"columns": t["columns"][:200], "source": rel, "source_kind": kind,
                                 "entity_kind": t["kind"], "orm": t.get("orm", "")},
                })
            repo_id = db.upsert_repo(repo, root, repo == self.kernel.repo_name)
            db.replace_file(repo_id, vrel, "schema", hashlib.sha1(fingerprint_src).hexdigest(), len(fingerprint_src),
                            len(nodes), 0, nodes, [])
            summary[kind] += 1
            tables_total += len(entities)
        pruned = 0
        for f in db.list_files():
            if f.path.startswith(SCHEMA_PREFIX) and f.path not in kept.get(f.repo_name, set()):
                db.delete_file(f.id)
                pruned += 1
        return {"sources": summary, "entities": tables_total, "pruned": pruned}
