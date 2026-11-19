"""Semantic diff: compare the symbol-level AST of the working tree against any git ref.

Instead of line hunks it reports which functions, classes, methods, interfaces and HTTP routes were
added, removed, renamed, changed signature (potentially breaking) or changed only in their body.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from aether_core.ast_indexer import IgnoreRules, detect_language, parse_file

SYMBOL_KINDS = frozenset({"function", "method", "class", "route", "interface", "type", "struct", "enum", "trait"})
PARSEABLE = frozenset({"python", "typescript", "javascript", "go", "rust"})
_WS = re.compile(r"\s+")


class SemanticDiffError(RuntimeError):
    """Invalid ref / not a git repository; message is user-facing."""


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if not git:
        raise SemanticDiffError("git executable not found on PATH")
    proc = subprocess.run([git, "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)
    if check and proc.returncode != 0:
        raise SemanticDiffError(proc.stderr.strip() or proc.stdout.strip() or f"git {' '.join(args)} failed")
    return proc


@dataclass(slots=True)
class _Sym:
    kind: str
    qualname: str
    name: str
    signature: str
    body_hash: str
    shape_hash: str
    line: int


def _norm_body(body: str) -> str:
    return _WS.sub(" ", body.replace("\r\n", "\n")).strip()


def _symbols(text: str, rel: str, language: str) -> dict[tuple[str, str], _Sym]:
    parsed = parse_file(text.replace("\r\n", "\n"), rel, language)
    out: dict[tuple[str, str], _Sym] = {}
    for n in parsed.nodes:
        if n["kind"] not in SYMBOL_KINDS:
            continue
        body = _norm_body(n.get("body", ""))
        # "shape" = body with the symbol's own name removed: identical shapes => rename.
        shape = re.sub(rf"\b{re.escape(n['name'])}\b", "§", body) if n["name"] else body
        out[(n["kind"], n["qualname"])] = _Sym(
            kind=n["kind"], qualname=n["qualname"], name=n["name"], signature=n.get("signature", ""),
            body_hash=hashlib.sha1(body.encode("utf-8")).hexdigest(),
            shape_hash=hashlib.sha1(shape.encode("utf-8")).hexdigest() if body else "",
            line=int(n.get("start_line", 1)),
        )
    return out


def _is_public(sym: _Sym) -> bool:
    if sym.kind == "route":
        return True
    last = sym.name.rsplit(".", 1)[-1]
    return not last.startswith("_") or (last.startswith("__") and last.endswith("__"))


def changed_files(root: Path, ref: str) -> list[tuple[str, str, str]]:
    """Return (status, old_path, new_path) for every file differing between ``ref`` and the working tree."""
    _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    proc = _git(root, "diff", "--name-status", "-M", "--no-color", ref, "--")
    out: list[tuple[str, str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][0]
        if status == "R" and len(parts) >= 3:
            out.append(("R", parts[1], parts[2]))
        elif status in {"M", "A", "D", "T"}:
            out.append((status if status != "T" else "M", parts[1], parts[1]))
    untracked = _git(root, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    rules = IgnoreRules(root)
    for rel in untracked:
        rel = rel.strip()
        if rel and not rules.ignored(rel, False) and not rel.startswith(".aether/"):
            out.append(("A", rel, rel))
    return out


def semantic_diff(root: Path, ref: str = "HEAD", paths: list[str] | None = None) -> dict[str, Any]:
    root = root.resolve()
    if not (root / ".git").exists():
        raise SemanticDiffError(f"{root} is not a git repository")
    if not ref or ref.startswith("-"):
        raise SemanticDiffError("invalid ref")
    wanted = {p.replace("\\", "/").strip("/") for p in paths} if paths else None
    report_files: list[dict[str, Any]] = []
    breaking: list[dict[str, Any]] = []
    totals = {"added": 0, "removed": 0, "signature_changed": 0, "modified": 0, "renamed": 0}
    skipped: list[str] = []
    for status, old_path, new_path in changed_files(root, ref):
        if wanted and old_path not in wanted and new_path not in wanted:
            continue
        language = detect_language(Path(new_path))
        if language not in PARSEABLE:
            skipped.append(new_path)
            continue
        old_text = ""
        if status in {"M", "D", "R"}:
            shown = _git(root, "show", f"{ref}:{old_path}", check=False)
            old_text = shown.stdout if shown.returncode == 0 else ""
        new_text = ""
        if status != "D":
            target = root / Path(*PurePosixPath(new_path).parts)
            try:
                new_text = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                new_text = ""
        before = _symbols(old_text, old_path, language) if old_text else {}
        after = _symbols(new_text, new_path, language) if new_text else {}
        added_keys = [k for k in after if k not in before]
        removed_keys = [k for k in before if k not in after]
        renamed: list[dict[str, Any]] = []
        for rk in list(removed_keys):
            old = before[rk]
            if not old.shape_hash:
                continue
            match = next((ak for ak in added_keys if after[ak].kind == old.kind and after[ak].shape_hash == old.shape_hash), None)
            if match:
                renamed.append({"kind": old.kind, "from": old.qualname, "to": after[match].qualname, "line": after[match].line})
                removed_keys.remove(rk)
                added_keys.remove(match)
        sig_changed: list[dict[str, Any]] = []
        modified: list[dict[str, Any]] = []
        for key in after:
            if key not in before:
                continue
            a, b = before[key], after[key]
            if a.signature != b.signature:
                sig_changed.append({"kind": b.kind, "symbol": b.qualname, "before": a.signature, "after": b.signature,
                                    "line": b.line})
            elif a.body_hash != b.body_hash:
                modified.append({"kind": b.kind, "symbol": b.qualname, "line": b.line})
        added = [{"kind": after[k].kind, "symbol": after[k].qualname, "signature": after[k].signature, "line": after[k].line}
                 for k in added_keys]
        removed = [{"kind": before[k].kind, "symbol": before[k].qualname, "signature": before[k].signature}
                   for k in removed_keys]
        for k in removed_keys:
            if _is_public(before[k]):
                breaking.append({"file": new_path, "change": "removed", "kind": before[k].kind, "symbol": before[k].qualname})
        for s in sig_changed:
            if _is_public(after[(s["kind"], s["symbol"])]):
                breaking.append({"file": new_path, "change": "signature", "kind": s["kind"], "symbol": s["symbol"],
                                 "before": s["before"], "after": s["after"]})
        for r in renamed:
            last = r["from"].rsplit(".", 1)[-1]
            if not last.startswith("_"):
                breaking.append({"file": new_path, "change": "renamed", "kind": r["kind"], "symbol": r["from"], "to": r["to"]})
        if not (added or removed or sig_changed or modified or renamed):
            continue
        totals["added"] += len(added)
        totals["removed"] += len(removed)
        totals["signature_changed"] += len(sig_changed)
        totals["modified"] += len(modified)
        totals["renamed"] += len(renamed)
        report_files.append({
            "path": new_path, "old_path": old_path if old_path != new_path else None,
            "status": {"M": "modified", "A": "added", "D": "deleted", "R": "renamed"}[status],
            "language": language, "added": added, "removed": removed, "renamed": renamed,
            "signature_changed": sig_changed, "modified": modified,
        })
    return {"ref": ref, "files": report_files, "summary": {**totals, "files": len(report_files)},
            "breaking": breaking, "non_code_files_changed": sorted(skipped)[:100]}
