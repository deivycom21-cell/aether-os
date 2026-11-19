"""Test skeleton generator: public API of a file -> a runnable pytest (Python) or vitest (JS/TS) module.

Generated tests are real and pass on the current code:
* **signature locks** – fail when a public function/method changes its parameters (API regression guard);
* **smoke calls** – only for functions whose every required parameter has a sample-able annotation
  (int/float/str/bool/bytes/list/dict/tuple/set/Optional[...]);
* **route probes** – FastAPI/Flask routes are requested through the framework test client and must
  not answer with a 5xx.
Extend the generated assertions with domain-specific expectations.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from aether_core.ast_indexer import HTTP_METHODS, python_module_name
from aether_core.database import normalize_rel_path

_SAMPLES: dict[str, str] = {
    "int": "1", "float": "1.0", "str": '"sample"', "bool": "True", "bytes": 'b"sample"', "list": "[]",
    "dict": "{}", "tuple": "()", "set": "set()", "None": "None", "Any": "None", "object": "object()",
    "Path": 'Path("sample")', "complex": "1j", "bytearray": "bytearray()", "frozenset": "frozenset()",
}
_TS_SAMPLES: dict[str, str] = {"number": "1", "string": '"sample"', "boolean": "true", "any": "undefined",
                               "unknown": "undefined", "object": "{}", "bigint": "1n"}


class TestGenError(RuntimeError):
    """User-facing generation failure (unsupported language, file not found)."""


def sample_for(annotation: ast.expr | None) -> str | None:
    """Python literal for a type annotation, or None when no safe sample exists."""
    if annotation is None:
        return None
    text = ast.unparse(annotation).replace("typing.", "")
    if text in _SAMPLES:
        return _SAMPLES[text]
    if "|" in text:
        parts = [p.strip() for p in text.split("|")]
        if "None" in parts:
            return "None"
        for p in parts:
            if p in _SAMPLES:
                return _SAMPLES[p]
        return None
    m = re.match(r"^(Optional)\[.+\]$", text)
    if m:
        return "None"
    m = re.match(r"^(list|List|Sequence|Iterable)\[.*\]$", text)
    if m:
        return "[]"
    m = re.match(r"^(dict|Dict|Mapping)\[.*\]$", text)
    if m:
        return "{}"
    m = re.match(r"^(tuple|Tuple)\[.*\]$", text)
    if m:
        return "()"
    m = re.match(r"^(set|Set|frozenset)\[.*\]$", text)
    if m:
        return "set()"
    m = re.match(r"^Literal\[(.+?)(?:,|\])", text)
    if m:
        return m.group(1)
    return None


@dataclass(slots=True)
class PyFunc:
    name: str
    params: list[str]
    required_samples: list[tuple[str, str | None]]
    is_async: bool
    owner: str = ""
    decorators: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PyRoute:
    method: str
    path: str
    owner: str
    handler: str


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef, skip_first: bool) -> tuple[list[str], list[tuple[str, str | None]]]:
    a = fn.args
    positional = [*a.posonlyargs, *a.args]
    if skip_first and positional:
        positional = positional[1:]
    names = [p.arg for p in positional]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    names += [k.arg for k in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    n_defaults = len(a.defaults)
    required: list[tuple[str, str | None]] = []
    for i, p in enumerate(positional):
        has_default = i >= len(positional) - n_defaults if n_defaults else False
        if not has_default:
            required.append((p.arg, sample_for(p.annotation)))
    for k, d in zip(a.kwonlyargs, a.kw_defaults):
        if d is None:
            required.append((k.arg, sample_for(k.annotation)))
    return names, required


def analyze_python(source: str) -> dict[str, Any]:
    tree = ast.parse(source)
    funcs: list[PyFunc] = []
    classes: list[dict[str, Any]] = []
    routes: list[PyRoute] = []
    app_vars: dict[str, str] = {}
    prefixes: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            ctor = ast.unparse(node.value.func).rsplit(".", 1)[-1]
            if ctor in {"FastAPI", "APIRouter", "Flask", "Blueprint"}:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        app_vars[t.id] = ctor
                        for kw in node.value.keywords:
                            if kw.arg in {"prefix", "url_prefix"} and isinstance(kw.value, ast.Constant):
                                prefixes[t.id] = str(kw.value.value)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            route_found = False
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and isinstance(dec.func.value, ast.Name):
                    owner, attr = dec.func.value.id, dec.func.attr.lower()
                    if owner in app_vars and attr in HTTP_METHODS and dec.args and isinstance(dec.args[0], ast.Constant):
                        routes.append(PyRoute(attr.upper(), prefixes.get(owner, "") + str(dec.args[0].value), owner, node.name))
                        route_found = True
            if node.name.startswith("_"):
                continue
            names, required = _params(node, skip_first=False)
            funcs.append(PyFunc(node.name, names, required, isinstance(node, ast.AsyncFunctionDef),
                                decorators=[ast.unparse(d) for d in node.decorator_list] if not route_found else ["route"]))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            methods: list[PyFunc] = []
            init_required: list[tuple[str, str | None]] = []
            is_dataclass = any("dataclass" in ast.unparse(d) for d in node.decorator_list)
            fields: list[tuple[str, str | None]] = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    decos = [ast.unparse(d) for d in item.decorator_list]
                    static = "staticmethod" in decos
                    names, required = _params(item, skip_first=not static)
                    if item.name == "__init__":
                        init_required = required
                    elif not item.name.startswith("_") and "property" not in decos:
                        methods.append(PyFunc(item.name, names, required, isinstance(item, ast.AsyncFunctionDef),
                                              owner=node.name, decorators=decos))
                elif is_dataclass and isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.value is None:
                    fields.append((item.target.id, sample_for(item.annotation)))
            if is_dataclass and not init_required:
                init_required = fields
            bases = [ast.unparse(b) for b in node.bases]
            classes.append({"name": node.name, "methods": methods, "init_required": init_required, "bases": bases})
    return {"functions": funcs, "classes": classes, "routes": routes, "apps": app_vars}


def _call_args(required: list[tuple[str, str | None]]) -> str | None:
    if any(s is None for _, s in required):
        return None
    return ", ".join(f"{name}={sample}" for name, sample in required)


def render_pytest(rel_path: str, source: str, root_depth: int) -> tuple[str, dict[str, int]]:
    info = analyze_python(source)
    module = python_module_name(rel_path)
    alias = "target"
    lines = [
        f'"""Generated by `aether gen-tests {rel_path}` — signature locks, smoke calls and route probes.',
        "",
        "Signature tests fail when the public API changes; extend smoke tests with real expectations.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import asyncio",
        "import inspect",
        "import sys",
        "from pathlib import Path",
        "",
        "import pytest",
        "",
        f"ROOT = Path(__file__).resolve().parents[{root_depth}]",
        "if str(ROOT) not in sys.path:",
        "    sys.path.insert(0, str(ROOT))",
        "",
        f"import {module} as {alias}  # noqa: E402",
        "",
    ]
    counts = {"signature": 0, "smoke": 0, "routes": 0, "classes": 0}

    def params_of(obj: str) -> str:
        return f"[p for p in inspect.signature({obj}).parameters]"

    for fn in info["functions"]:
        if fn.decorators and fn.decorators != ["route"]:
            continue  # arbitrary decorators may replace the function object; only lock plain/route callables
        expected = [p.lstrip("*") for p in fn.params]
        lines += [
            "",
            f"def test_{fn.name}_signature_is_stable() -> None:",
            f"    assert {params_of(f'{alias}.{fn.name}')} == {expected!r}",
        ]
        counts["signature"] += 1
        if fn.decorators:
            continue
        args = _call_args(fn.required_samples)
        if args is None:
            continue
        call = f"{alias}.{fn.name}({args})"
        lines += ["", f"def test_{fn.name}_smoke() -> None:"]
        lines.append(f"    result = asyncio.run({call})" if fn.is_async else f"    result = {call}")
        lines.append("    assert result is not NotImplemented")
        counts["smoke"] += 1
    for cls in info["classes"]:
        counts["classes"] += 1
        cname = cls["name"]
        lines += ["", "", f"class Test{cname}:"]
        lines += [f"    def test_is_importable(self) -> None:", f"        assert inspect.isclass({alias}.{cname})"]
        args = _call_args(cls["init_required"])
        can_build = args is not None and not any(b.endswith(("ABC", "Protocol", "BaseModel")) or b in {"ABC", "Enum"} for b in cls["bases"])
        if can_build:
            lines += ["", "    def test_can_be_constructed(self) -> None:",
                      f"        instance = {alias}.{cname}({args})", f"        assert isinstance(instance, {alias}.{cname})"]
            counts["smoke"] += 1
        for m in cls["methods"]:
            expected = [p.lstrip("*") for p in m.params]
            is_static = "staticmethod" in m.decorators
            first = [] if is_static else (["cls"] if "classmethod" in m.decorators else ["self"])
            sig_target = f"{alias}.{cname}.{m.name}"
            if "classmethod" in m.decorators:
                assertion = f"        assert {params_of(sig_target)} == {expected!r}"
            else:
                assertion = f"        assert {params_of(sig_target)} == {[*first, *expected]!r}"
            lines += ["", f"    def test_{m.name}_signature_is_stable(self) -> None:", assertion]
            counts["signature"] += 1
    if info["routes"]:
        app_owner = next((v for v, kind in info["apps"].items() if kind in {"FastAPI", "Flask"}), None)
        router_owner = next((v for v, kind in info["apps"].items() if kind in {"APIRouter", "Blueprint"}), None)
        framework = info["apps"].get(app_owner or router_owner or "", "FastAPI")
        router_included = False
        if framework in {"FastAPI", "APIRouter"}:
            lines += ["", "", "@pytest.fixture()", "def client():",
                      '    testclient = pytest.importorskip("fastapi.testclient")']
            router_included = bool(router_owner and app_owner and f"include_router({router_owner}" in source)
            if router_owner and not router_included and any(r.owner == router_owner for r in info["routes"]):
                lines += ["    from fastapi import FastAPI", "", "    app = FastAPI()",
                          f"    app.include_router({alias}.{router_owner})"]
                if app_owner:
                    lines.append(f"    app.mount('/__root__', {alias}.{app_owner})")
                lines.append("    with testclient.TestClient(app, raise_server_exceptions=False) as c:")
            else:
                lines.append(f"    with testclient.TestClient({alias}.{app_owner}, raise_server_exceptions=False) as c:")
            lines.append("        yield c")
        else:
            lines += ["", "", "@pytest.fixture()", "def client():",
                      f"    {alias}.{app_owner or router_owner}.testing = True",
                      f"    return {alias}.{app_owner or router_owner}.test_client()"]
        seen: set[str] = set()
        for r in info["routes"]:
            mounted = framework in {"FastAPI", "APIRouter"} and router_owner and app_owner and not router_included
            if mounted and r.owner == app_owner:
                url = "/__root__" + re.sub(r"\{[^}]+\}", "1", r.path)
            else:
                url = re.sub(r"\{[^}]+\}|<[^>]+>", "1", r.path)
            test_name = re.sub(r"\W+", "_", f"{r.method}_{r.path}").strip("_").lower()
            if test_name in seen:
                continue
            seen.add(test_name)
            lines += ["", f"def test_route_{test_name}_does_not_error(client) -> None:",
                      f"    response = client.{r.method.lower()}({url!r})",
                      "    assert response.status_code < 500"]
            counts["routes"] += 1
    lines.append("")
    return "\n".join(lines), counts


_TS_FUNC = re.compile(r"^export\s+(?:default\s+)?(async\s+)?function\s+([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\(([^)]*)\)", re.M)
_TS_ARROW = re.compile(r"^export\s+const\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(async\s+)?\(([^)]*)\)\s*(?::[^=]+)?=>", re.M)
_TS_CLASS = re.compile(r"^export\s+(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.M)


def _ts_params(raw: str) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    depth = 0
    cur = ""
    for ch in raw:
        if ch in "<{[(":
            depth += 1
        elif ch in ">}])":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    params: list[tuple[str, str | None]] = []
    for p in out:
        p = p.strip()
        if not p or "=" in p or p.split(":")[0].strip().endswith("?"):
            continue
        name, _, typ = p.partition(":")
        typ = typ.strip()
        sample = _TS_SAMPLES.get(typ)
        if typ.endswith("[]"):
            sample = "[]"
        params.append((name.strip().lstrip("."), sample))
    return params


def render_vitest(rel_path: str, source: str, import_path: str) -> tuple[str, dict[str, int]]:
    funcs = [(m.group(2), bool(m.group(1)), _ts_params(m.group(3))) for m in _TS_FUNC.finditer(source)]
    funcs += [(m.group(1), bool(m.group(2)), _ts_params(m.group(3))) for m in _TS_ARROW.finditer(source)]
    classes = [m.group(1) for m in _TS_CLASS.finditer(source)]
    names = sorted({f[0] for f in funcs} | set(classes))
    counts = {"signature": 0, "smoke": 0, "routes": 0, "classes": len(classes)}
    lines = [
        f"// Generated by `aether gen-tests {rel_path}` — export locks and smoke calls (vitest).",
        'import { describe, expect, it } from "vitest";',
        f"import {{ {', '.join(names)} }} from {import_path!r};" if names else f"import * as target from {import_path!r};",
        "",
        f"describe({PurePosixPath(rel_path).name!r}, () => {{",
    ]
    if not names:
        lines.append('  it("module loads", () => { expect(target).toBeDefined(); });')
    for name, is_async, params in funcs:
        lines.append(f'  it("{name} keeps its arity", () => {{ expect(typeof {name}).toBe("function"); '
                     f"expect({name}.length).toBe({len(params)}); }});")
        counts["signature"] += 1
        if all(s is not None for _, s in params):
            args = ", ".join(str(s) for _, s in params)
            if is_async:
                lines.append(f'  it("{name} resolves", async () => {{ await {name}({args}); }});')
            else:
                lines.append(f'  it("{name} runs", () => {{ expect(() => {name}({args})).not.toThrow(); }});')
            counts["smoke"] += 1
    for cls in classes:
        lines.append(f'  it("{cls} is a class", () => {{ expect(typeof {cls}).toBe("function"); }});')
    lines += ["});", ""]
    return "\n".join(lines), counts


def default_output(rel_path: str) -> str:
    p = PurePosixPath(rel_path)
    if p.suffix in {".py", ".pyi"}:
        return f"tests/test_{p.stem}_generated.py"
    return f"tests/{p.stem}.generated.test{'.ts' if p.suffix in {'.ts', '.tsx', '.mts', '.cts'} else '.js'}"


def generate(root: Path, rel_path: str, out_rel: str | None = None) -> dict[str, Any]:
    rel = normalize_rel_path(rel_path)
    src_path = (root / Path(*PurePosixPath(rel).parts)).resolve()
    if root.resolve() not in src_path.parents or not src_path.is_file():
        raise TestGenError(f"file not found inside the project: {rel_path}")
    source = src_path.read_text(encoding="utf-8", errors="replace")
    out = (out_rel or default_output(rel)).replace("\\", "/")
    suffix = src_path.suffix.lower()
    if suffix in {".py", ".pyi"}:
        depth = len(PurePosixPath(out).parts) - 1
        try:
            content, counts = render_pytest(rel, source, depth)
        except SyntaxError as exc:
            raise TestGenError(f"cannot parse {rel}: {exc}") from exc
        framework = "pytest"
    elif suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".mts"}:
        out_dir = PurePosixPath(out).parent
        target = PurePosixPath(rel).with_suffix("")
        up = [".."] * len(out_dir.parts)
        import_path = "/".join([*up, *target.parts]) if up else "./" + target.as_posix()
        content, counts = render_vitest(rel, source, import_path)
        framework = "vitest"
    else:
        raise TestGenError(f"unsupported language for test generation: {suffix or rel}")
    return {"source": rel, "output": out, "framework": framework, "content": content, "counts": counts}
