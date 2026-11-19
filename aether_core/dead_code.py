"""Dead / orphan code detector.

A symbol is reported when BOTH hold:
1. no resolved graph edge (call / extends / import) points at it, and
2. its identifier never appears in any indexed file except at its own definition(s) — this textual
   cross-check catches callbacks, ``getattr`` strings, registries, ``__all__`` and config references
   that a static call graph misses.

Framework entrypoints are excluded: route handlers and other registration decorators
(``@app.get``, ``@router.post``, ``@app.command``, ``@mcp.tool``, ``@pytest.fixture``...), tests,
``[project.scripts]`` entrypoints, ``main``, dunder methods, visitor/handler hooks and Next.js exports.

Every finding carries a confidence: ``high`` (private or non-exported helpers), ``medium`` (public
functions, classes and methods), ``low`` (subclasses / overrides that may be invoked polymorphically).
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from aether_core.ast_indexer import is_virtual_path

if TYPE_CHECKING:
    from aether_core.database import AetherDB, NodeRecord

CANDIDATE_KINDS = ("function", "method", "class")
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
REGISTRATION_DECORATOR = re.compile(
    r"(?:^|\.)(?:get|post|put|delete|patch|head|options|route|api_route|websocket|command|callback|tool|resource|"
    r"prompt|fixture|middleware|on_event|exception_handler|listener|listens_for|task|hookimpl|receiver|register|"
    r"subscribe|handler|on|setter|getter|deleter|validator|field_validator|model_validator|root_validator|"
    r"cached_property|property|abstractmethod|overload|override|lru_cache|cache|app|group|shared_task|job)\b",
    re.I,
)
FRAMEWORK_NAMES = frozenset({
    "main", "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "default", "getServerSideProps",
    "getStaticProps", "getStaticPaths", "generateMetadata", "generateStaticParams", "middleware", "loader", "action",
    "setUp", "tearDown", "setUpClass", "tearDownClass", "setup_module", "teardown_module", "init", "run", "compose",
    "render", "constructor", "connectedCallback", "disconnectedCallback", "ngOnInit", "ngOnDestroy", "componentDidMount",
    "componentWillUnmount", "generic_visit", "handle", "dispatch", "build", "lifespan",
})
HOOK_PREFIXES = ("visit_", "depart_", "on_", "action_", "watch_", "validate_", "test_", "pytest_", "do_", "handle_")
NON_CODE_LANGS = frozenset({"markdown", "text"})


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name
    return (
        any(part in {"tests", "test", "__tests__", "spec", "e2e"} for part in p.parts[:-1])
        or name.startswith("test_") or name.endswith(("_test.py", "_test.go", "conftest.py"))
        or ".test." in name or ".spec." in name
    )


def script_entrypoints(root: Path) -> set[str]:
    """Function names referenced by ``[project.scripts]`` / ``[project.gui-scripts]`` in pyproject.toml."""
    py = root / "pyproject.toml"
    if not py.is_file():
        return set()
    text = py.read_text(encoding="utf-8", errors="replace")
    return {m.group(1).rsplit(".", 1)[-1] for m in re.finditer(r"=\s*[\"'][\w.]+:([\w.]+)[\"']", text)}


def _excluded(node: NodeRecord, entrypoints: set[str]) -> str | None:
    name = node.name
    if is_test_path(node.path):
        return "test"
    if name.startswith("__") and name.endswith("__"):
        return "dunder"
    if name in FRAMEWORK_NAMES or name in entrypoints:
        return "entrypoint"
    if name.startswith(HOOK_PREFIXES):
        return "hook"
    decorators = [str(d) for d in node.metadata.get("decorators") or []]
    if any(REGISTRATION_DECORATOR.search(d.split("(", 1)[0]) for d in decorators):
        return "registered"
    if PurePosixPath(node.path).name in {"route.ts", "route.js", "page.tsx", "page.jsx", "layout.tsx", "middleware.ts"}:
        return "framework file"
    return None


_ROUTE_PARAM = re.compile(r"\{[^}/]+\}|:[A-Za-z_]\w*|<[^>/]+>|\[[^\]/]+\]")


def _route_referenced(route: str, owner: tuple[str, str], texts: dict[tuple[str, str], str]) -> bool:
    """True when another non-test file mentions the route literally (params may be any template)."""
    parts = _ROUTE_PARAM.split(route)
    pattern = r"[^\s\"'`/]+".join(re.escape(p) for p in parts)
    rx = re.compile(r"[\"'`}]" + pattern + r"(?:[\"'`?#]|$)", re.M)
    return any(key != owner and not is_test_path(key[1]) and rx.search(text) for key, text in texts.items())


def find_dead_code(db: AetherDB, repo_roots: dict[str, Path], min_confidence: str = "medium",
                   limit: int = 200) -> dict[str, Any]:
    min_rank = CONFIDENCE_ORDER.get(min_confidence, 1)
    files = [f for f in db.list_files() if not is_virtual_path(f.path)]
    ident_counts: Counter[str] = Counter()
    texts: dict[tuple[str, str], str] = {}
    for f in files:
        if f.language in NON_CODE_LANGS:
            continue
        root = repo_roots.get(f.repo_name)
        if root is None:
            continue
        try:
            text = (root / Path(*PurePosixPath(f.path).parts)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ident_counts.update(_IDENT.findall(text))
        texts[(f.repo_name, f.path)] = text

    nodes = [n for kind in CANDIDATE_KINDS for n in db.nodes_by_kind(kind) if not is_virtual_path(n.path)]
    defs: Counter[str] = Counter(n.name for n in nodes)
    method_defs: Counter[str] = Counter(n.name for n in nodes if n.kind == "method")
    referenced_nodes = {int(r["dst_node_id"]) for r in db.query(
        "SELECT DISTINCT dst_node_id FROM edges WHERE dst_node_id IS NOT NULL AND kind IN ('calls', 'extends', 'imports')")}
    entrypoints: set[str] = set()
    for root in repo_roots.values():
        entrypoints |= script_entrypoints(root)

    findings: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    for n in nodes:
        reason = _excluded(n, entrypoints)
        if reason:
            excluded[reason] += 1
            continue
        if n.id in referenced_nodes:
            continue
        if ident_counts.get(n.name, 0) > defs[n.name]:
            continue
        bases = n.signature.split("(", 1)[1] if n.kind == "class" and "(" in n.signature else ""
        exported = n.body.lstrip().startswith("export") or n.signature.lstrip().startswith("export")
        if (n.kind == "class" and bases.strip(" )")) or method_defs[n.name] > 1:
            confidence = "low"
        elif n.name.startswith("_") or (n.path.endswith((".ts", ".tsx", ".js", ".jsx")) and not exported and n.kind == "function"):
            confidence = "high"
        else:
            confidence = "medium"
        if CONFIDENCE_ORDER[confidence] > min_rank:
            continue
        findings.append({
            "repo": n.repo_name, "file": n.path, "symbol": n.qualname, "name": n.name, "kind": n.kind,
            "line": n.start_line, "lines": max(1, n.end_line - n.start_line + 1), "confidence": confidence,
            "signature": n.signature,
        })
    findings.sort(key=lambda d: (CONFIDENCE_ORDER[d["confidence"]], -d["lines"], d["file"], d["line"]))

    has_consumers = bool(db.scalar("SELECT COUNT(*) FROM edges WHERE kind = 'http_call'") or 0)
    unconsumed: list[dict[str, Any]] = []
    if has_consumers:
        consumed = {int(r["dst_node_id"]) for r in db.query(
            "SELECT DISTINCT dst_node_id FROM edges WHERE kind = 'http_call' AND dst_node_id IS NOT NULL")}
        for r in db.nodes_by_kind("route"):
            path = str(r.metadata.get("path") or "")
            if r.id in consumed or is_test_path(r.path) or path in {"", "/"}:
                continue
            if _route_referenced(path, (r.repo_name, r.path), texts):
                continue  # e.g. fetch() inside an HTML <script> block or a template literal
            unconsumed.append({"repo": r.repo_name, "file": r.path, "method": r.metadata.get("method"),
                                   "path": r.metadata.get("path"), "handler": r.metadata.get("handler", r.qualname),
                                   "line": r.start_line})
    dead_lines = sum(f["lines"] for f in findings)
    return {
        "dead_symbols": findings[:limit],
        "total": len(findings),
        "dead_lines_estimate": dead_lines,
        "by_confidence": dict(Counter(f["confidence"] for f in findings)),
        "unconsumed_routes": unconsumed[:limit],
        "unconsumed_routes_note": (
            "Routes with no HTTP consumer in any linked repository (they may still serve external clients)."
            if has_consumers else "No frontend HTTP calls are indexed; link a client repo to audit endpoint usage."
        ),
        "excluded": dict(excluded),
        "min_confidence": min_confidence,
    }
