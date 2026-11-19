"""Multi-language structural indexer.

* Python: native ``ast`` (classes, functions, methods, imports, calls, FastAPI/Flask routes, HTTP calls).
* TypeScript / JavaScript: tolerant regex + brace matching (functions, arrows, classes, interfaces,
  imports, Express/Next.js routes, ``fetch``/``axios`` calls).
* Go / Rust: regex parsers for funcs, types, imports and HTTP routes.
* JSON (``package.json`` deps & scripts), Dockerfile stages/ports, Markdown sections, SQL tables,
  YAML/TOML and other text files as module nodes.

After every indexing pass ``GraphResolver`` links edges to concrete files/nodes, including
cross-repository links between frontend HTTP calls and backend routes.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import pickle
import re
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from aether_core.context_engine import estimate_tokens
from aether_core.database import AetherDB

MAX_FILE_BYTES = 512 * 1024
MAX_BODY_CHARS = 4000

DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".aether", ".venv", "venv", "env", ".env.d", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox", "dist", "build",
    ".next", ".nuxt", ".svelte-kit", "target", "out", "coverage", ".idea", ".vscode", "site-packages",
    ".gradle", "vendor", ".terraform", ".cache", ".parcel-cache", ".turbo", "bower_components",
    "egg-info",
}
SENSITIVE_FILE_PATTERNS = ("*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.p12", "*.pfx", ".env", ".env.*")

LANG_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go", ".rs": "rust",
    ".json": "json", ".md": "markdown", ".mdx": "markdown",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".sql": "sql",
    ".html": "html", ".css": "css", ".scss": "css", ".sh": "shell", ".ps1": "powershell",
    ".ini": "ini", ".cfg": "ini", ".txt": "text", ".vue": "vue", ".svelte": "svelte",
    ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".php": "php", ".cs": "csharp",
}
LANG_BY_NAME = {"dockerfile": "dockerfile", "makefile": "makefile", "containerfile": "dockerfile"}
SKIP_FILE_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock", "go.sum"}

HTTP_METHODS = ("get", "post", "put", "delete", "patch", "head", "options")


def detect_language(path: Path) -> str | None:
    name = path.name.lower()
    if name in LANG_BY_NAME or name.startswith("dockerfile"):
        return LANG_BY_NAME.get(name, "dockerfile")
    return LANG_BY_EXT.get(path.suffix.lower())


def normalize_route(path: str) -> str:
    """Normalize route templates so ``/users/{id}``, ``/users/:id`` and ``/users/[id]`` match."""
    p = urlparse(path).path if "://" in path else path.split("?")[0].split("#")[0]
    p = re.sub(r"\$\{[^}]*\}", ":param", p)
    p = re.sub(r"\{[^}/]+\}|:[A-Za-z_][\w]*|\[\.{0,3}[^\]/]+\]|<[^>/]+>", ":param", p)
    p = "/" + p.strip("/")
    return p.lower()


# ---------------------------------------------------------------------------- ignore rules


class IgnoreRules:
    def __init__(self, root: Path, extra_patterns: Iterable[str] = ()) -> None:
        self.root = root
        self.patterns: list[tuple[str, bool, bool]] = []  # (pattern, dir_only, anchored)
        for name in (".gitignore", ".aetherignore"):
            f = root / name
            if f.is_file():
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    self._add(line)
        for p in extra_patterns:
            self._add(p)

    def _add(self, line: str) -> None:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            return
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = line.startswith("/") or "/" in line
        self.patterns.append((line.lstrip("/"), dir_only, anchored))

    def ignored(self, rel: str, is_dir: bool) -> bool:
        name = rel.rsplit("/", 1)[-1]
        if is_dir and (name in DEFAULT_IGNORE_DIRS or name.endswith(".egg-info")):
            return True
        for pat, dir_only, anchored in self.patterns:
            if dir_only and not is_dir:
                continue
            if anchored:
                if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat + "/**"):
                    return True
            elif fnmatch.fnmatch(name, pat):
                return True
        return False


def iter_source_files(root: Path, rules: IgnoreRules | None = None) -> Iterator[Path]:
    rules = rules or IgnoreRules(root)
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            rel = entry.relative_to(root).as_posix()
            if entry.is_dir():
                if not rules.ignored(rel, True):
                    stack.append(entry)
                continue
            if entry.name in SKIP_FILE_NAMES or rules.ignored(rel, False):
                continue
            if any(fnmatch.fnmatch(entry.name, p) for p in SENSITIVE_FILE_PATTERNS):
                continue  # never index secret material (env_shield audits these separately)
            if detect_language(entry) is None:
                continue
            try:
                if entry.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield entry


# ---------------------------------------------------------------------------- parsed structures


@dataclass(slots=True)
class ParsedFile:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)

    def node(self, kind: str, name: str, qualname: str | None = None, *, signature: str = "",
             docstring: str = "", body: str = "", start_line: int = 1, end_line: int | None = None,
             **metadata: Any) -> None:
        self.nodes.append(
            {
                "kind": kind,
                "name": name,
                "qualname": qualname or name,
                "signature": signature[:500],
                "docstring": docstring[:2000],
                "body": body[:MAX_BODY_CHARS],
                "start_line": start_line,
                "end_line": end_line if end_line is not None else start_line,
                "metadata": metadata,
            }
        )

    def edge(self, kind: str, dst_name: str, src_qualname: str | None = None, **metadata: Any) -> None:
        self.edges.append({"kind": kind, "dst_name": dst_name, "src_qualname": src_qualname, "metadata": metadata})


def _lines_slice(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[max(0, start - 1) : end])


def python_module_name(rel_path: str) -> str:
    p = PurePosixPath(rel_path)
    parts = list(p.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# ---------------------------------------------------------------------------- python


_URL_LITERAL = re.compile(r"^(https?://[^\s'\"]+|/api/[^\s'\"]*|/v\d+/[^\s'\"]*)$")


class _PyVisitor(ast.NodeVisitor):
    def __init__(self, parsed: ParsedFile, lines: list[str], module: str, rel_path: str) -> None:
        self.p = parsed
        self.lines = lines
        self.module = module
        self.rel_path = rel_path
        self.scope: list[str] = []
        self.router_prefixes: dict[str, str] = {}

    # -- traversal: statements only (expression trees are scanned once per function in _visit_func),
    #    which is several times faster than NodeVisitor's default full-tree walk.
    _STMT_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")

    def generic_visit(self, node: ast.AST) -> None:
        for fname in self._STMT_FIELDS:
            value = getattr(node, fname, None)
            if not isinstance(value, list):
                continue
            for child in value:
                if isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)):
                    self.visit(child)

    # -- helpers
    def _qual(self, name: str) -> str:
        return ".".join([self.module, *self.scope, name]) if self.module else ".".join([*self.scope, name])

    def _resolve_relative(self, level: int, module: str | None) -> str:
        if level == 0:
            return module or ""
        base_parts = self.module.split(".") if self.module else []
        is_pkg = self.rel_path.endswith("__init__.py")
        if not is_pkg:
            base_parts = base_parts[:-1]
        if level > 1:
            base_parts = base_parts[: len(base_parts) - (level - 1)] if level - 1 <= len(base_parts) else []
        return ".".join([*base_parts, *(module.split(".") if module else [])])

    # -- visitors
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.p.edge("imports", alias.name, None, line=node.lineno, candidates=[alias.name])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = self._resolve_relative(node.level, node.module)
        for alias in node.names:
            candidates = [f"{base}.{alias.name}" if base else alias.name, base]
            self.p.edge("imports", candidates[0] if alias.name != "*" else base, None,
                        line=node.lineno, candidates=[c for c in candidates if c], symbol=alias.name)

    def visit_Assign(self, node: ast.Assign) -> None:
        # router = APIRouter(prefix="/api/users")
        if isinstance(node.value, ast.Call):
            fname = _call_name(node.value.func)
            if fname in {"APIRouter", "Blueprint", "FastAPI", "Flask"}:
                prefix = ""
                for kw in node.value.keywords:
                    if kw.arg in {"prefix", "url_prefix", "root_path"} and isinstance(kw.value, ast.Constant):
                        prefix = str(kw.value.value)
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.router_prefixes[t.id] = prefix
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qual = self._qual(node.name)
        bases = [ast.unparse(b) for b in node.bases]
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        self.p.node(
            "class", node.name, qual,
            signature=f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}",
            docstring=ast.get_docstring(node) or "",
            body=_lines_slice(self.lines, node.lineno, end),
            start_line=node.lineno, end_line=end,
            decorators=[ast.unparse(d) for d in node.decorator_list],
        )
        for b in bases:
            self.p.edge("extends", b.split("[")[0], qual, line=node.lineno)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qual = self._qual(node.name)
        kind = "method" if self.scope and not self.scope[-1].startswith("<") else "function"
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
        signature = f"{prefix} {node.name}({ast.unparse(node.args)}){ret}"
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        start = min([d.lineno for d in node.decorator_list] + [node.lineno])
        self.p.node(
            kind, node.name, qual, signature=signature,
            docstring=ast.get_docstring(node) or "",
            body=_lines_slice(self.lines, start, end),
            start_line=start, end_line=end,
            decorators=[ast.unparse(d) for d in node.decorator_list],
        )
        # Routes: @app.get("/x"), @router.post("/x"), @app.route("/x", methods=[...])
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            attr = dec.func.attr.lower()
            if attr not in (*HTTP_METHODS, "route", "api_route", "websocket"):
                continue
            if not dec.args or not isinstance(dec.args[0], ast.Constant) or not isinstance(dec.args[0].value, str):
                continue
            owner = ast.unparse(dec.func.value)
            path = self.router_prefixes.get(owner, "") + dec.args[0].value
            methods = [attr.upper()] if attr in HTTP_METHODS else ["ANY"]
            for kw in dec.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    methods = [str(e.value).upper() for e in kw.value.elts if isinstance(e, ast.Constant)]
            for m in methods:
                self.p.node(
                    "route", f"{m} {path}", f"{qual}::{m} {path}",
                    signature=f"{m} {path} -> {node.name}",
                    docstring=ast.get_docstring(node) or "",
                    start_line=start, end_line=end,
                    method=m, path=path, normalized=normalize_route(path), handler=qual, framework="python",
                )
        # Calls + outbound HTTP (nested defs are scanned when they are visited themselves)
        called: set[str] = set()
        for sub in _iter_calls(node):
            name = _call_name(sub.func)
            if name and not name.startswith("_"):
                called.add(name)
            if isinstance(sub.func, ast.Attribute) and sub.func.attr.lower() in HTTP_METHODS and sub.args:
                a0 = sub.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str) and _URL_LITERAL.match(a0.value):
                    self._api_call(sub.func.attr.upper(), a0.value, qual, sub.lineno)
        for name in sorted(called)[:80]:
            self.p.edge("calls", name, qual)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _api_call(self, method: str, url: str, src_qual: str, line: int) -> None:
        norm = normalize_route(url)
        self.p.node("api_call", f"{method} {norm}", f"{src_qual}::call {method} {norm}",
                    signature=f"{method} {url}", start_line=line, method=method, url=url, normalized=norm)
        self.p.edge("http_call", f"{method} {norm}", src_qual, method=method, normalized=norm, url=url, line=line)

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func


_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _iter_calls(fn: ast.AST) -> Iterator[ast.Call]:
    """Yield Call nodes inside ``fn`` without descending into nested functions/classes."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        if isinstance(n, _NESTED_SCOPES):
            continue
        if isinstance(n, ast.Call):
            yield n
        stack.extend(ast.iter_child_nodes(n))


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def parse_python(text: str, rel_path: str) -> ParsedFile:
    parsed = ParsedFile()
    lines = text.splitlines()
    module = python_module_name(rel_path)
    try:
        tree = ast.parse(text, filename=rel_path)
    except (SyntaxError, ValueError) as exc:
        parsed.node("module", module or rel_path, module or rel_path,
                    docstring=f"(unparseable: {exc.__class__.__name__})", body=_lines_slice(lines, 1, 60),
                    start_line=1, end_line=len(lines), parse_error=str(exc)[:200])
        return parsed
    parsed.node("module", module.rsplit(".", 1)[-1] if module else rel_path, module or rel_path,
                docstring=ast.get_docstring(tree) or "", body=_lines_slice(lines, 1, 40),
                start_line=1, end_line=max(1, len(lines)), language="python")
    _PyVisitor(parsed, lines, module, rel_path).visit(tree)
    return parsed


# ---------------------------------------------------------------------------- brace languages


def find_block_end(text: str, start: int) -> int:
    """Return the index just after the ``}`` closing the first ``{`` at/after ``start``."""
    i = text.find("{", start)
    if i < 0:
        nl = text.find("\n", start)
        return len(text) if nl < 0 else nl
    depth = 0
    n = len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = n if nl < 0 else nl
            continue
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            i = n if close < 0 else close + 2
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _line_at(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _leading_doc(text: str, pos: int) -> str:
    """Grab a JSDoc/``///``/``//`` comment block immediately above ``pos``."""
    before = text[:pos].rstrip()
    if before.endswith("*/"):
        start = before.rfind("/**")
        if start >= 0 and before.count("\n", start) < 40:
            body = before[start + 3 : -2]
            return "\n".join(ln.strip().lstrip("*").strip() for ln in body.splitlines()).strip()
    doc_lines: list[str] = []
    for ln in reversed(before.splitlines()):
        s = ln.strip()
        if s.startswith("///") or s.startswith("//"):
            doc_lines.append(s.lstrip("/").strip())
        else:
            break
    return "\n".join(reversed(doc_lines)).strip()


_JS_IMPORT = re.compile(
    r"""(?:import\s+(?:[\w*{}\s,]+?\s+from\s+)?|export\s+[\w*{}\s,]+?\s+from\s+|require\(\s*|import\(\s*)['"]([^'"]+)['"]"""
)
_JS_FUNC = re.compile(r"(?m)^[ \t]*(export\s+)?(default\s+)?(async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*(<[^>]*>)?\s*\(([^)]*)\)")
_JS_ARROW = re.compile(
    r"(?m)^[ \t]*(export\s+)?(const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=\n]+)?=\s*(async\s+)?(?:\(([^)]*)\)|([A-Za-z_$][\w$]*))\s*(?::\s*[^=\n]+)?=>"
)
_JS_CLASS = re.compile(r"(?m)^[ \t]*(export\s+)?(default\s+)?(abstract\s+)?class\s+([A-Za-z_$][\w$]*)(?:\s*<[^>]*>)?(?:\s+extends\s+([\w$.]+))?")
_JS_METHOD = re.compile(r"(?m)^[ \t]+(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+)*([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*(?::\s*[^{;\n]+)?\{")
_JS_IFACE = re.compile(r"(?m)^[ \t]*(export\s+)?(interface|type|enum)\s+([A-Za-z_$][\w$]*)")
_JS_ROUTE = re.compile(r"""\b(app|router|server|api|route|r)\.(get|post|put|delete|patch|all|options)\(\s*['"`](/[^'"`]*)['"`]""")
_JS_FETCH = re.compile(r"""\bfetch\(\s*(['"`])([^'"`]+)\1(\s*,\s*\{[^}]{0,300}\})?""")
_JS_AXIOS = re.compile(r"""\b(?:axios|api|http|client|\$http)\.(get|post|put|delete|patch)\(\s*(['"`])([^'"`]+)\2""")
_NEXT_HANDLER = re.compile(r"(?m)^export\s+(?:async\s+)?(?:function|const)\s+(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b")
_JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "constructor", "super", "else", "do", "try"}


def _clean_url(raw: str) -> str | None:
    url = re.sub(r"^\$\{[^}]*\}", "", raw.strip())
    if url.startswith("http://") or url.startswith("https://"):
        parsed = urlparse(re.sub(r"\$\{[^}]*\}", "x", url))
        return parsed.path or "/"
    if url.startswith("/"):
        return url
    return None


def _next_route_from_path(rel_path: str) -> str | None:
    p = PurePosixPath(rel_path)
    parts = list(p.parts)
    for anchor in ("pages", "app"):
        if anchor in parts:
            idx = len(parts) - 1 - parts[::-1].index(anchor)
            sub = parts[idx + 1 :]
            if anchor == "pages":
                if not sub or sub[0] != "api":
                    return None
                last = PurePosixPath(sub[-1]).stem
                segs = sub[:-1] + ([] if last == "index" else [last])
            else:
                if not sub or PurePosixPath(sub[-1]).stem != "route":
                    return None
                segs = [s for s in sub[:-1] if not (s.startswith("(") and s.endswith(")"))]
            return "/" + "/".join(segs)
    return None


def parse_js_ts(text: str, rel_path: str, language: str) -> ParsedFile:
    parsed = ParsedFile()
    lines = text.splitlines()
    module = PurePosixPath(rel_path).with_suffix("").as_posix()
    parsed.node("module", PurePosixPath(rel_path).stem, module, docstring=_leading_doc(text, 0) if text.startswith("/**") else "",
                body=_lines_slice(lines, 1, 40), start_line=1, end_line=max(1, len(lines)), language=language)
    for m in _JS_IMPORT.finditer(text):
        parsed.edge("imports", m.group(1), None, line=_line_at(text, m.start()))

    class_ranges: list[tuple[int, int, str]] = []
    for m in _JS_CLASS.finditer(text):
        name = m.group(4)
        end = find_block_end(text, m.end())
        sl, el = _line_at(text, m.start()), _line_at(text, end)
        sig = m.group(0).strip()
        parsed.node("class", name, f"{module}.{name}", signature=sig, docstring=_leading_doc(text, m.start()),
                    body=text[m.start():end], start_line=sl, end_line=el)
        if m.group(5):
            parsed.edge("extends", m.group(5), f"{module}.{name}", line=sl)
        class_ranges.append((m.start(), end, name))
        body = text[m.start():end]
        for mm in _JS_METHOD.finditer(body):
            mname = mm.group(1)
            if mname in _JS_KEYWORDS:
                continue
            abs_start = m.start() + mm.start()
            mend = find_block_end(text, m.start() + mm.end() - 1)
            parsed.node("method", mname, f"{module}.{name}.{mname}", signature=f"{mname}({mm.group(2).strip()})",
                        docstring=_leading_doc(text, abs_start), body=text[abs_start:mend],
                        start_line=_line_at(text, abs_start), end_line=_line_at(text, mend))

    def inside_class(pos: int) -> bool:
        return any(s <= pos < e for s, e, _ in class_ranges)

    for m in _JS_FUNC.finditer(text):
        if inside_class(m.start()):
            continue
        name = m.group(4)
        end = find_block_end(text, m.end())
        parsed.node("function", name, f"{module}.{name}",
                    signature=f"{'async ' if m.group(3) else ''}function {name}({m.group(6).strip()})",
                    docstring=_leading_doc(text, m.start()), body=text[m.start():end],
                    start_line=_line_at(text, m.start()), end_line=_line_at(text, end), exported=bool(m.group(1)))
    for m in _JS_ARROW.finditer(text):
        if inside_class(m.start()):
            continue
        name = m.group(3)
        rest = text[m.end(): m.end() + 3].lstrip()
        end = find_block_end(text, m.end()) if rest.startswith("{") else (text.find("\n", m.end()) if text.find("\n", m.end()) > 0 else len(text))
        params = m.group(5) if m.group(5) is not None else (m.group(6) or "")
        parsed.node("function", name, f"{module}.{name}", signature=f"{'async ' if m.group(4) else ''}const {name} = ({params.strip()}) =>",
                    docstring=_leading_doc(text, m.start()), body=text[m.start():end],
                    start_line=_line_at(text, m.start()), end_line=_line_at(text, end), exported=bool(m.group(1)))
    for m in _JS_IFACE.finditer(text):
        name = m.group(3)
        end = find_block_end(text, m.end()) if m.group(2) != "type" else text.find(";", m.end()) + 1 or len(text)
        parsed.node(m.group(2), name, f"{module}.{name}", signature=f"{m.group(2)} {name}",
                    docstring=_leading_doc(text, m.start()), body=text[m.start():end],
                    start_line=_line_at(text, m.start()), end_line=_line_at(text, end))

    # Express-style routes
    for m in _JS_ROUTE.finditer(text):
        method = m.group(2).upper()
        method = "ANY" if method == "ALL" else method
        path = m.group(3)
        ln = _line_at(text, m.start())
        parsed.node("route", f"{method} {path}", f"{module}::{method} {path}", signature=f"{method} {path}",
                    body=_lines_slice(lines, ln, ln + 15), start_line=ln, method=method, path=path,
                    normalized=normalize_route(path), framework="express")
    # Next.js file-system routes
    next_path = _next_route_from_path(rel_path)
    if next_path is not None:
        handlers = _NEXT_HANDLER.findall(text) or (["ANY"] if "/pages/" in f"/{rel_path}" else [])
        for method in handlers:
            parsed.node("route", f"{method} {next_path}", f"{module}::{method} {next_path}",
                        signature=f"{method} {next_path}", start_line=1, method=method, path=next_path,
                        normalized=normalize_route(next_path), framework="nextjs")
    # Outbound HTTP calls (cross-repo links)
    for m in _JS_FETCH.finditer(text):
        url = _clean_url(m.group(2))
        if not url:
            continue
        opts = m.group(3) or ""
        mm = re.search(r"""method\s*:\s*['"](\w+)['"]""", opts)
        method = mm.group(1).upper() if mm else "GET"
        _add_api_call(parsed, module, method, url, _line_at(text, m.start()))
    for m in _JS_AXIOS.finditer(text):
        url = _clean_url(m.group(3))
        if url:
            _add_api_call(parsed, module, m.group(1).upper(), url, _line_at(text, m.start()))
    return parsed


def _add_api_call(parsed: ParsedFile, module: str, method: str, url: str, line: int) -> None:
    norm = normalize_route(url)
    parsed.node("api_call", f"{method} {norm}", f"{module}::call {method} {norm}@{line}", signature=f"{method} {url}",
                start_line=line, method=method, url=url, normalized=norm)
    parsed.edge("http_call", f"{method} {norm}", None, method=method, normalized=norm, url=url, line=line)


# ---------------------------------------------------------------------------- go


_GO_IMPORT_BLOCK = re.compile(r"import\s*\(([^)]*)\)", re.S)
_GO_IMPORT_ONE = re.compile(r"""(?m)^import\s+(?:\w+\s+)?"([^"]+)\"""")
_GO_FUNC = re.compile(r"(?m)^func\s+(?:\(\s*\w*\s*\*?\s*([\w\[\]]+)\s*\)\s*)?([A-Za-z_]\w*)\s*(\[[^\]]*\])?\s*\(([^)]*)\)\s*([^{\n]*)")
_GO_TYPE = re.compile(r"(?m)^type\s+([A-Za-z_]\w*)\s+(struct|interface|func|[\w.\[\]*]+)")
_GO_ROUTE = re.compile(r"""\.(HandleFunc|Handle|GET|POST|PUT|DELETE|PATCH|Get|Post|Put|Delete|Patch|Any)\(\s*"(/[^"]*)\"""")


def parse_go(text: str, rel_path: str) -> ParsedFile:
    parsed = ParsedFile()
    lines = text.splitlines()
    pkg = re.search(r"(?m)^package\s+(\w+)", text)
    module = PurePosixPath(rel_path).with_suffix("").as_posix()
    parsed.node("module", pkg.group(1) if pkg else PurePosixPath(rel_path).stem, module,
                docstring=_leading_doc(text, pkg.start()) if pkg else "", body=_lines_slice(lines, 1, 30),
                start_line=1, end_line=max(1, len(lines)), language="go", package=pkg.group(1) if pkg else "")
    for block in _GO_IMPORT_BLOCK.finditer(text):
        for imp in re.findall(r'"([^"]+)"', block.group(1)):
            parsed.edge("imports", imp, None)
    for m in _GO_IMPORT_ONE.finditer(text):
        parsed.edge("imports", m.group(1), None)
    for m in _GO_FUNC.finditer(text):
        recv, name = m.group(1), m.group(2)
        end = find_block_end(text, m.end())
        qual = f"{module}.{recv}.{name}" if recv else f"{module}.{name}"
        parsed.node("method" if recv else "function", name, qual,
                    signature=f"func {'(' + recv + ') ' if recv else ''}{name}({m.group(4).strip()}) {m.group(5).strip()}".strip(),
                    docstring=_leading_doc(text, m.start()), body=text[m.start():end],
                    start_line=_line_at(text, m.start()), end_line=_line_at(text, end), receiver=recv or "")
    for m in _GO_TYPE.finditer(text):
        end = find_block_end(text, m.end()) if m.group(2) in {"struct", "interface"} else m.end()
        parsed.node("class" if m.group(2) == "struct" else "interface", m.group(1), f"{module}.{m.group(1)}",
                    signature=f"type {m.group(1)} {m.group(2)}", docstring=_leading_doc(text, m.start()),
                    body=text[m.start():end], start_line=_line_at(text, m.start()), end_line=_line_at(text, end))
    for m in _GO_ROUTE.finditer(text):
        verb = m.group(1).upper()
        method = "ANY" if verb in {"HANDLEFUNC", "HANDLE", "ANY"} else verb
        path = m.group(2)
        if " " in path:  # Go 1.22 pattern "GET /path"
            method, path = path.split(" ", 1)
        ln = _line_at(text, m.start())
        parsed.node("route", f"{method} {path}", f"{module}::{method} {path}", signature=f"{method} {path}",
                    start_line=ln, method=method, path=path, normalized=normalize_route(path), framework="go")
    return parsed


# ---------------------------------------------------------------------------- rust


_RS_USE = re.compile(r"(?m)^\s*(?:pub\s+)?use\s+([\w:]+)")
_RS_MOD = re.compile(r"(?m)^\s*(?:pub(?:\([\w:]+\))?\s+)?mod\s+(\w+)\s*;")
_RS_FN = re.compile(r"(?m)^[ \t]*(?:pub(?:\([\w:]+\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"\w+\"\s+)?fn\s+(\w+)\s*(<[^>]*>)?\s*\(([^)]*)\)\s*(->\s*[^{;\n]+)?")
_RS_TYPE = re.compile(r"(?m)^[ \t]*(?:pub(?:\([\w:]+\))?\s+)?(struct|enum|trait|union)\s+(\w+)")
_RS_IMPL = re.compile(r"(?m)^[ \t]*impl(?:<[^>]*>)?\s+(?:([\w:]+)(?:<[^>]*>)?\s+for\s+)?([\w:]+)")
_RS_ATTR_ROUTE = re.compile(r"""#\[(get|post|put|delete|patch)\(\s*"([^"]+)\"""")
_RS_ROUTE_CALL = re.compile(r"""\.route\(\s*"([^"]+)"\s*,\s*(?:web::)?(get|post|put|delete|patch|any)\b""")


def parse_rust(text: str, rel_path: str) -> ParsedFile:
    parsed = ParsedFile()
    lines = text.splitlines()
    module = PurePosixPath(rel_path).with_suffix("").as_posix()
    inner_doc = "\n".join(ln.strip()[3:].strip() for ln in lines[:30] if ln.strip().startswith("//!"))
    parsed.node("module", PurePosixPath(rel_path).stem, module, docstring=inner_doc, body=_lines_slice(lines, 1, 30),
                start_line=1, end_line=max(1, len(lines)), language="rust")
    for m in _RS_USE.finditer(text):
        parsed.edge("imports", m.group(1), None)
    for m in _RS_MOD.finditer(text):
        parsed.edge("imports", f"self::{m.group(1)}", None, mod_decl=True)
    impl_ranges: list[tuple[int, int, str]] = []
    for m in _RS_IMPL.finditer(text):
        end = find_block_end(text, m.end())
        impl_ranges.append((m.start(), end, m.group(2).split("::")[-1]))
        if m.group(1):
            parsed.edge("extends", m.group(1).split("::")[-1], None, implementor=m.group(2))
    for m in _RS_FN.finditer(text):
        name = m.group(1)
        owner = next((o for s, e, o in impl_ranges if s <= m.start() < e), "")
        end = find_block_end(text, m.end()) if text[m.end():m.end() + 200].lstrip().startswith(("{", "where")) else m.end()
        parsed.node("method" if owner else "function", name, f"{module}.{owner + '.' if owner else ''}{name}",
                    signature=f"fn {name}({m.group(3).strip()}) {(m.group(4) or '').strip()}".strip(),
                    docstring=_leading_doc(text, m.start()), body=text[m.start():end],
                    start_line=_line_at(text, m.start()), end_line=_line_at(text, end))
    for m in _RS_TYPE.finditer(text):
        end = find_block_end(text, m.end()) if "{" in text[m.end():m.end() + 120].split(";")[0] else text.find(";", m.end()) + 1
        parsed.node("class" if m.group(1) in {"struct", "union"} else m.group(1), m.group(2), f"{module}.{m.group(2)}",
                    signature=f"{m.group(1)} {m.group(2)}", docstring=_leading_doc(text, m.start()),
                    body=text[m.start():end], start_line=_line_at(text, m.start()), end_line=_line_at(text, end))
    for m in _RS_ATTR_ROUTE.finditer(text):
        method, path = m.group(1).upper(), m.group(2)
        parsed.node("route", f"{method} {path}", f"{module}::{method} {path}", signature=f"{method} {path}",
                    start_line=_line_at(text, m.start()), method=method, path=path, normalized=normalize_route(path), framework="actix")
    for m in _RS_ROUTE_CALL.finditer(text):
        method, path = m.group(2).upper(), m.group(1)
        parsed.node("route", f"{method} {path}", f"{module}::{method} {path}", signature=f"{method} {path}",
                    start_line=_line_at(text, m.start()), method=method, path=path, normalized=normalize_route(path), framework="axum")
    return parsed


# ---------------------------------------------------------------------------- data / infra files


def parse_json(text: str, rel_path: str) -> ParsedFile:
    parsed = ParsedFile()
    name = PurePosixPath(rel_path).name
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        parsed.node("module", name, rel_path, body=text[:1500], docstring="(invalid JSON)", language="json")
        return parsed
    keys = list(data.keys())[:40] if isinstance(data, dict) else []
    if name == "package.json" and isinstance(data, dict):
        pkg = str(data.get("name") or PurePosixPath(rel_path).parent.name or "package")
        parsed.node("package", pkg, f"npm:{pkg}", signature=f"{pkg}@{data.get('version', '0.0.0')}",
                    docstring=str(data.get("description") or ""), body=text[:1500], language="json")
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            for dep, ver in (data.get(section) or {}).items():
                parsed.node("dependency", dep, f"npm:{pkg}:{dep}", signature=f"{dep}@{ver}", section=section, version=str(ver))
                parsed.edge("depends_on", f"npm:{dep}", f"npm:{pkg}", version=str(ver), section=section)
        for script, cmd in (data.get("scripts") or {}).items():
            parsed.node("script", script, f"npm:{pkg}:script:{script}", signature=f"npm run {script}", body=str(cmd))
        return parsed
    parsed.node("module", name, rel_path, signature=f"keys: {', '.join(map(str, keys))}" if keys else "",
                body=text[:1500], language="json")
    return parsed


_DOCKER_FROM = re.compile(r"(?im)^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?")
_DOCKER_EXPOSE = re.compile(r"(?im)^\s*EXPOSE\s+(.+)$")
_DOCKER_CMD = re.compile(r"(?im)^\s*(CMD|ENTRYPOINT)\s+(.+)$")


def parse_dockerfile(text: str, rel_path: str) -> ParsedFile:
    parsed = ParsedFile()
    lines = text.splitlines()
    ports = [p for m in _DOCKER_EXPOSE.finditer(text) for p in m.group(1).split()]
    cmds = [f"{m.group(1)} {m.group(2).strip()}" for m in _DOCKER_CMD.finditer(text)]
    parsed.node("module", PurePosixPath(rel_path).name, rel_path, signature=f"EXPOSE {' '.join(ports)}" if ports else "",
                docstring="; ".join(cmds), body=text[:MAX_BODY_CHARS], start_line=1, end_line=max(1, len(lines)),
                language="dockerfile", ports=ports)
    for m in _DOCKER_FROM.finditer(text):
        image, stage = m.group(1), m.group(2) or m.group(1)
        parsed.node("docker_stage", stage, f"{rel_path}::{stage}", signature=f"FROM {image}" + (f" AS {m.group(2)}" if m.group(2) else ""),
                    start_line=_line_at(text, m.start()), image=image, ports=ports)
        parsed.edge("depends_on", f"image:{image}", f"{rel_path}::{stage}")
    return parsed


def parse_markdown(text: str, rel_path: str) -> ParsedFile:
    parsed = ParsedFile()
    lines = text.splitlines()
    heads = [(i + 1, len(m.group(1)), m.group(2).strip()) for i, ln in enumerate(lines)
             if (m := re.match(r"^(#{1,4})\s+(.+)$", ln))]
    parsed.node("module", PurePosixPath(rel_path).name, rel_path, docstring=heads[0][2] if heads else "",
                body=_lines_slice(lines, 1, 25), start_line=1, end_line=max(1, len(lines)), language="markdown")
    for idx, (ln, level, title) in enumerate(heads):
        end = heads[idx + 1][0] - 1 if idx + 1 < len(heads) else len(lines)
        parsed.node("section", title, f"{rel_path}#{title}", signature=f"{'#' * level} {title}",
                    body=_lines_slice(lines, ln, end), start_line=ln, end_line=end, level=level)
    return parsed


def parse_sql(text: str, rel_path: str) -> ParsedFile:
    parsed = ParsedFile()
    lines = text.splitlines()
    parsed.node("module", PurePosixPath(rel_path).name, rel_path, body=_lines_slice(lines, 1, 30),
                start_line=1, end_line=max(1, len(lines)), language="sql")
    for m in re.finditer(r"(?i)create\s+(?:virtual\s+)?(table|view|index)\s+(?:if\s+not\s+exists\s+)?([\w.\"]+)", text):
        name = m.group(2).strip('"')
        end = text.find(";", m.end())
        end = len(text) if end < 0 else end + 1
        parsed.node(m.group(1).lower(), name, f"{rel_path}::{name}", signature=f"CREATE {m.group(1).upper()} {name}",
                    body=text[m.start():end], start_line=_line_at(text, m.start()), end_line=_line_at(text, end))
    return parsed


def parse_generic(text: str, rel_path: str, language: str) -> ParsedFile:
    parsed = ParsedFile()
    lines = text.splitlines()
    first_comment = next((ln.strip().lstrip("#;/ -").strip() for ln in lines[:10] if ln.strip().startswith(("#", "//", ";", "--"))), "")
    parsed.node("module", PurePosixPath(rel_path).name, rel_path, docstring=first_comment,
                body=_lines_slice(lines, 1, 80), start_line=1, end_line=max(1, len(lines)), language=language)
    if language == "yaml":
        keys = re.findall(r"(?m)^([A-Za-z_][\w\-]*):", text)
        if keys:
            parsed.nodes[0]["signature"] = f"keys: {', '.join(keys[:30])}"
    if language == "toml":
        tables = re.findall(r"(?m)^\[([^\]]+)\]", text)
        if tables:
            parsed.nodes[0]["signature"] = f"tables: {', '.join(tables[:30])}"
    return parsed


def parse_file(text: str, rel_path: str, language: str) -> ParsedFile:
    if language == "python":
        return parse_python(text, rel_path)
    if language in {"typescript", "javascript", "vue", "svelte"}:
        return parse_js_ts(text, rel_path, language)
    if language == "go":
        return parse_go(text, rel_path)
    if language == "rust":
        return parse_rust(text, rel_path)
    if language == "json":
        return parse_json(text, rel_path)
    if language == "dockerfile":
        return parse_dockerfile(text, rel_path)
    if language == "markdown":
        return parse_markdown(text, rel_path)
    if language == "sql":
        return parse_sql(text, rel_path)
    return parse_generic(text, rel_path, language)


# ---------------------------------------------------------------------------- indexer

PARALLEL_THRESHOLD = 64  # below this, process start-up costs more than it saves (spawn on Windows)


def _parse_job(job: tuple[str, str, str, str, int]) -> tuple[ParsedFile, int, str]:
    """Parse one file; never raises (a single bad file must not abort indexing)."""
    text, rel, language, _, _ = job
    try:
        return parse_file(text, rel, language), estimate_tokens(text), ""
    except Exception as exc:  # noqa: BLE001
        return parse_generic(text, rel, language), estimate_tokens(text), f"{exc.__class__.__name__}: {exc}"


def _parse_many(jobs: list[tuple[str, str, str, str, int]]) -> list[tuple[ParsedFile, int, str]]:
    workers = min(8, os.cpu_count() or 1)
    if len(jobs) >= PARALLEL_THRESHOLD and workers > 1 and not os.environ.get("AETHER_NO_PARALLEL"):
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                return list(pool.map(_parse_job, jobs, chunksize=max(1, len(jobs) // (workers * 4))))
        except (OSError, RuntimeError, BrokenProcessPool, pickle.PicklingError):
            pass  # e.g. restricted environments without multiprocessing: fall back to sequential
    return [_parse_job(j) for j in jobs]



@dataclass(slots=True)
class IndexStats:
    repo: str
    scanned: int = 0
    indexed: int = 0
    unchanged: int = 0
    removed: int = 0
    nodes: int = 0
    edges: int = 0
    errors: list[str] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo, "scanned": self.scanned, "indexed": self.indexed, "unchanged": self.unchanged,
            "removed": self.removed, "nodes": self.nodes, "edges": self.edges, "errors": self.errors[:20],
            "duration_ms": round(self.duration_ms, 1),
        }


class Indexer:
    def __init__(self, db: AetherDB) -> None:
        self.db = db

    def index_repo(self, root: Path, name: str | None = None, primary: bool = False, force: bool = False) -> IndexStats:
        root = root.resolve()
        repo_name = name or root.name
        t0 = time.perf_counter()
        stats = IndexStats(repo=repo_name)
        repo_id = self.db.upsert_repo(repo_name, root, primary)
        known = self.db.get_file_hashes(repo_id)
        seen: set[str] = set()
        jobs: list[tuple[str, str, str, str, int]] = []  # (text, rel, language, sha, size)
        for path in iter_source_files(root):
            rel = path.relative_to(root).as_posix()
            stats.scanned += 1
            seen.add(rel)
            try:
                raw = path.read_bytes()
            except OSError as exc:
                stats.errors.append(f"{rel}: {exc}")
                continue
            if b"\x00" in raw[:2048]:
                continue
            sha = hashlib.sha1(raw).hexdigest()
            if not force and rel in known and known[rel][1] == sha:
                stats.unchanged += 1
                continue
            jobs.append((raw.decode("utf-8", errors="replace"), rel, detect_language(path) or "text", sha, len(raw)))

        for (text, rel, language, sha, size), (parsed, tokens, error) in zip(jobs, _parse_many(jobs)):
            if error:
                stats.errors.append(f"{rel}: {error}")
            self.db.replace_file(repo_id, rel, language, sha, size, text.count("\n") + 1, tokens, parsed.nodes, parsed.edges)
            stats.indexed += 1
            stats.nodes += len(parsed.nodes)
            stats.edges += len(parsed.edges)
        for rel, (fid, _) in known.items():
            if rel in seen:
                continue
            on_disk = root / rel
            # Plugin-managed records (e.g. .env.example names) stay while their file exists.
            if not on_disk.is_file() or detect_language(on_disk) is not None:
                self.db.delete_file(fid)
                stats.removed += 1
        self.db.mark_repo_indexed(repo_id)
        stats.duration_ms = (time.perf_counter() - t0) * 1000
        return stats

    def index_single_file(self, root: Path, path: Path, repo_name: str) -> bool:
        """Re-index one file (used by the watcher). Returns True if the graph changed."""
        root = root.resolve()
        path = path.resolve()
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            return False
        repo_id = self.db.upsert_repo(repo_name, root)
        rules = IgnoreRules(root)
        parts = rel.split("/")
        if any(rules.ignored("/".join(parts[: i + 1]), True) for i in range(len(parts) - 1)) or rules.ignored(rel, False):
            return False
        existing = self.db.file_by_path(rel, repo_name)
        if not path.exists():
            if existing and existing.path == rel:
                self.db.delete_file(existing.id)
                return True
            return False
        language = detect_language(path)
        if language is None or path.stat().st_size > MAX_FILE_BYTES:
            return False
        raw = path.read_bytes()
        sha = hashlib.sha1(raw).hexdigest()
        if existing and existing.path == rel and existing.sha1 == sha:
            return False
        text = raw.decode("utf-8", errors="replace")
        parsed = parse_file(text, rel, language)
        self.db.replace_file(repo_id, rel, language, sha, len(raw), text.count("\n") + 1,
                             estimate_tokens(text), parsed.nodes, parsed.edges)
        return True


# ---------------------------------------------------------------------------- resolver


class GraphResolver:
    """Links unresolved edges to files/nodes across every indexed repository."""

    JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")

    def __init__(self, db: AetherDB) -> None:
        self.db = db
        self._file_repo: dict[int, int] = {}

    def resolve(self) -> int:
        files = self.db.list_files()
        by_repo_path: dict[tuple[int, str], int] = {(f.repo_id, f.path): f.id for f in files}
        py_modules: dict[str, list[int]] = defaultdict(list)
        go_dirs: dict[str, list[int]] = defaultdict(list)
        for f in files:
            if f.language == "python":
                mod = python_module_name(f.path)
                py_modules[mod].append(f.id)
                if mod.startswith("src."):
                    py_modules[mod[4:]].append(f.id)
            elif f.language == "go":
                go_dirs[str(PurePosixPath(f.path).parent)].append(f.id)
        file_repo = {f.id: f.repo_id for f in files}
        self._file_repo = file_repo

        routes: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
        for n in self.db.nodes_by_kind("route"):
            routes[str(n.metadata.get("normalized", ""))].append((str(n.metadata.get("method", "ANY")), n.file_id, n.id))
        symbols: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for row in self.db.query("SELECT id, file_id, name FROM nodes WHERE kind IN ('function','class','method','interface','type','trait','struct','enum')"):
            symbols[row["name"]].append((int(row["file_id"]), int(row["id"])))
        npm_pkgs = {n.qualname: n.file_id for n in self.db.nodes_by_kind("package")}

        resolved = 0
        imports_of: dict[int, set[int]] = defaultdict(set)
        for row in self.db.query("SELECT src_file_id, dst_file_id FROM edges WHERE kind = 'imports' AND dst_file_id IS NOT NULL"):
            imports_of[int(row["src_file_id"])].add(int(row["dst_file_id"]))

        pending = self.db.unresolved_edges()
        # imports first so that calls/extends can use the import graph
        pending.sort(key=lambda r: 0 if r["kind"] == "imports" else 1)
        updates: list[tuple[int | None, int | None, int]] = []
        for e in pending:
            kind = e["kind"]
            src_fid = int(e["src_file_id"])
            meta = json.loads(e["metadata"] or "{}")
            dst_file: int | None = None
            dst_node: int | None = None
            if kind == "imports":
                dst_file = self._resolve_import(e, meta, py_modules, go_dirs, by_repo_path)
                if dst_file is not None and dst_file != src_fid:
                    imports_of[src_fid].add(dst_file)
            elif kind == "http_call":
                norm = str(meta.get("normalized", ""))
                method = str(meta.get("method", "ANY")).upper()
                cands = routes.get(norm, [])
                best = [c for c in cands if c[0] in (method, "ANY") or method == "ANY"] or cands
                if best:
                    # prefer a route in another repo (cross-repo link) and then same repo
                    best.sort(key=lambda c: 0 if file_repo.get(c[1]) != file_repo.get(src_fid) else 1)
                    _, dst_file, dst_node = best[0]
            elif kind in {"calls", "extends"}:
                name = str(e["dst_name"]).split(".")[-1]
                for fid, nid in symbols.get(name, []):
                    if fid in imports_of.get(src_fid, set()):
                        dst_file, dst_node = fid, nid
                        break
            elif kind == "depends_on" and str(e["dst_name"]).startswith("npm:"):
                dst_file = npm_pkgs.get(str(e["dst_name"]))
            if dst_file is not None and dst_file != src_fid:
                updates.append((dst_file, dst_node, int(e["id"])))
        if updates:
            with self.db.transaction() as conn:
                conn.executemany("UPDATE edges SET dst_file_id = ?, dst_node_id = ? WHERE id = ?", updates)
            resolved = len(updates)
        return resolved

    def _resolve_import(self, e: Any, meta: dict[str, Any], py_modules: dict[str, list[int]],
                        go_dirs: dict[str, list[int]], by_repo_path: dict[tuple[int, str], int]) -> int | None:
        lang = e["src_language"]
        repo_id = int(e["repo_id"])
        dst = str(e["dst_name"])
        if lang == "python":
            for cand in meta.get("candidates") or [dst]:
                ids = py_modules.get(cand)
                if ids:
                    same = [i for i in ids if self._file_repo.get(i) == repo_id]
                    return (same or ids)[0]
            return None
        if lang in {"typescript", "javascript", "vue", "svelte"}:
            if not dst.startswith("."):
                alias = re.sub(r"^(@/|~/)", "src/", dst)
                if alias == dst:
                    return None
                base = PurePosixPath(alias)
            else:
                base = PurePosixPath(e["src_path"]).parent / dst
            norm = _posix_normpath(str(base))
            candidates = [norm] + [norm + ext for ext in self.JS_EXTS] + [f"{norm}/index{ext}" for ext in self.JS_EXTS]
            if norm.endswith(".js"):
                stem = norm[:-3]
                candidates += [stem + ".ts", stem + ".tsx"]
            for c in candidates:
                fid = by_repo_path.get((repo_id, c))
                if fid is not None:
                    return fid
            return None
        if lang == "go":
            for d, ids in go_dirs.items():
                if d != "." and (dst.endswith("/" + d) or dst == d):
                    return sorted(ids)[0]
            return None
        if lang == "rust":
            parts = dst.replace("crate::", "").replace("self::", "").replace("super::", "").split("::")
            src_dir = PurePosixPath(e["src_path"]).parent
            roots = [src_dir, PurePosixPath("src")]
            for r in roots:
                for k in range(len(parts), 0, -1):
                    sub = "/".join(parts[:k])
                    for c in (f"{r}/{sub}.rs", f"{r}/{sub}/mod.rs"):
                        fid = by_repo_path.get((repo_id, _posix_normpath(c)))
                        if fid is not None:
                            return fid
            return None
        return None


def _posix_normpath(p: str) -> str:
    out: list[str] = []
    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if out:
                out.pop()
            continue
        out.append(part)
    return "/".join(out)
