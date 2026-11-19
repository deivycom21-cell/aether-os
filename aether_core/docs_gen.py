"""Living docs: a Mermaid module-dependency diagram and an API endpoint table generated from the graph.

Modules are directories (``aether_core``, ``src/api``) or top-level files; edges aggregate every resolved
``imports``/``calls``/``extends``/``http_call`` edge between files of different modules.
"""

from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from aether_core.ast_indexer import is_virtual_path

if TYPE_CHECKING:
    from aether_core.database import AetherDB

DOC_LANGS = frozenset({"markdown", "text", "json", "yaml", "toml", "ini", "html", "css"})
_MERMAID_ID = re.compile(r"[^A-Za-z0-9_]")


def module_of(path: str) -> str:
    """``aether_core/kernel.py`` -> ``aether_core``; ``src/api/client.ts`` -> ``src/api``; ``setup.py`` -> ``setup``."""
    parts = PurePosixPath(path).parts
    if len(parts) == 1:
        return PurePosixPath(path).stem
    if parts[0] in {"src", "lib", "app", "packages", "apps", "internal", "pkg", "cmd"} and len(parts) > 2:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def mermaid_id(repo: str, module: str) -> str:
    return "m_" + _MERMAID_ID.sub("_", f"{repo}__{module}")


def _label(text: str) -> str:
    return text.replace('"', "'").replace("<", "‹").replace(">", "›")


@dataclass(slots=True)
class ModuleInfo:
    repo: str
    name: str
    files: int = 0
    symbols: int = 0
    lines: int = 0
    languages: Counter[str] = field(default_factory=Counter)

    def to_dict(self) -> dict[str, Any]:
        return {"repo": self.repo, "module": self.name, "files": self.files, "symbols": self.symbols, "lines": self.lines,
                "languages": [lang for lang, _ in self.languages.most_common(3)]}


def module_graph(db: AetherDB, include_docs: bool = False) -> dict[str, Any]:
    files = {f.id: f for f in db.list_files() if not is_virtual_path(f.path)
             and (include_docs or f.language not in DOC_LANGS)}
    symbol_counts = {int(r["file_id"]): int(r["n"]) for r in db.query(
        "SELECT file_id, COUNT(*) AS n FROM nodes WHERE kind NOT IN ('module', 'section') GROUP BY file_id")}
    modules: dict[tuple[str, str], ModuleInfo] = {}
    file_module: dict[int, tuple[str, str]] = {}
    for f in files.values():
        key = (f.repo_name, module_of(f.path))
        info = modules.setdefault(key, ModuleInfo(f.repo_name, key[1]))
        info.files += 1
        info.symbols += symbol_counts.get(f.id, 0)
        info.lines += f.line_count
        info.languages[f.language] += 1
        file_module[f.id] = key
    weights: Counter[tuple[tuple[str, str], tuple[str, str], str]] = Counter()
    for r in db.query("SELECT src_file_id, dst_file_id, kind FROM edges WHERE dst_file_id IS NOT NULL AND dst_file_id != src_file_id"):
        a, b = file_module.get(int(r["src_file_id"])), file_module.get(int(r["dst_file_id"]))
        if a and b and a != b:
            kind = "http" if r["kind"] == "http_call" else "code"
            weights[(a, b, kind)] += 1
    edges = [{"from": {"repo": a[0], "module": a[1]}, "to": {"repo": b[0], "module": b[1]}, "kind": kind, "weight": w}
             for (a, b, kind), w in weights.most_common()]
    return {"modules": sorted((m.to_dict() for m in modules.values()), key=lambda d: (d["repo"], -d["symbols"])),
            "edges": edges}


def endpoints(db: AetherDB) -> list[dict[str, Any]]:
    consumers: dict[int, list[str]] = defaultdict(list)
    for r in db.query(
        "SELECT e.dst_node_id, f.path, rp.name AS repo FROM edges e JOIN files f ON f.id = e.src_file_id "
        "JOIN repos rp ON rp.id = f.repo_id WHERE e.kind = 'http_call' AND e.dst_node_id IS NOT NULL"
    ):
        consumers[int(r["dst_node_id"])].append(f"{r['repo']}:{r['path']}")
    out = []
    for n in db.nodes_by_kind("route"):
        out.append({
            "repo": n.repo_name, "method": str(n.metadata.get("method", "ANY")), "path": str(n.metadata.get("path", n.name)),
            "handler": str(n.metadata.get("handler", n.qualname)), "file": n.path, "line": n.start_line,
            "summary": (n.docstring.strip().splitlines() or [""])[0][:120],
            "consumers": sorted(set(consumers.get(n.id, []))),
        })
    out.sort(key=lambda e: (e["repo"], e["path"], e["method"]))
    return out


def render_mermaid(graph: dict[str, Any], max_edges: int = 60) -> str:
    lines = ["graph LR"]
    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in graph["modules"]:
        by_repo[m["repo"]].append(m)
    multi = len(by_repo) > 1
    for repo, mods in by_repo.items():
        if multi:
            lines.append(f'  subgraph {mermaid_id(repo, "repo")}["{_label(repo)}"]')
        for m in mods:
            label = f"{m['module']}<br/>{m['files']} files · {m['symbols']} symbols"
            lines.append(f'    {mermaid_id(repo, m["module"])}["{_label(label).replace("‹br/›", "<br/>")}"]')
        if multi:
            lines.append("  end")
    for e in graph["edges"][:max_edges]:
        a = mermaid_id(e["from"]["repo"], e["from"]["module"])
        b = mermaid_id(e["to"]["repo"], e["to"]["module"])
        arrow = f"-. HTTP x{e['weight']} .->" if e["kind"] == "http" else f"-->|{e['weight']}|"
        lines.append(f"  {a} {arrow} {b}")
    return "\n".join(lines)


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_docs(db: AetherDB, project: str, index_version: int, max_edges: int = 60) -> dict[str, Any]:
    graph = module_graph(db)
    eps = endpoints(db)
    md: list[str] = [
        f"# {project} — living architecture docs",
        "",
        f"_Generated by `aether docs` on {time.strftime('%Y-%m-%d')} from index v{index_version}. "
        "Re-run after structural changes; manual edits to this file are overwritten._",
        "",
        "## Module dependency graph",
        "",
        "```mermaid",
        render_mermaid(graph, max_edges),
        "```",
        "",
        "## Modules",
        "",
        "| repo | module | files | symbols | lines | languages |",
        "|---|---|---:|---:|---:|---|",
    ]
    for m in graph["modules"]:
        md.append(f"| {_md_cell(m['repo'])} | `{_md_cell(m['module'])}` | {m['files']} | {m['symbols']} | {m['lines']} | "
                  f"{', '.join(m['languages'])} |")
    md += ["", f"## API endpoints ({len(eps)})", ""]
    if eps:
        md += ["| method | path | handler | location | consumers |", "|---|---|---|---|---|"]
        for e in eps:
            md.append(f"| `{e['method']}` | `{_md_cell(e['path'])}` | `{_md_cell(e['handler'])}` | "
                      f"{_md_cell(e['repo'])}:{_md_cell(e['file'])}:{e['line']} | "
                      f"{_md_cell(', '.join(e['consumers'])) or '—'} |")
    else:
        md.append("_No HTTP routes detected._")
    md.append("")
    return {"markdown": "\n".join(md), "modules": len(graph["modules"]), "module_edges": len(graph["edges"]),
            "endpoints": len(eps)}
