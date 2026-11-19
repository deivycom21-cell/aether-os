"""Plugin contract and a safe dynamic loader.

Built-in plugins live in this package. Project-local plugins live in ``.aether/plugins/*.py`` and
are only loaded when (1) their stem is listed in ``trusted_plugins`` in ``.aether/config.json`` and
(2) a static AST audit finds no dangerous primitives (``eval``/``exec``/``subprocess``/sockets...).
Every plugin runs isolated in a worker thread with a timeout; failures never break indexing.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import pkgutil
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aether_core.kernel import AetherKernel

PLUGIN_TIMEOUT_S = 30.0

FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}
FORBIDDEN_MODULES = {"subprocess", "socket", "ctypes", "pickle", "marshal", "shelve", "multiprocessing", "pty", "winreg"}
FORBIDDEN_ATTRS = {"system", "popen", "spawnl", "spawnv", "execv", "execl", "startfile", "fork"}


@dataclass(slots=True)
class PluginResult:
    name: str
    ok: bool
    summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "summary": self.summary, "error": self.error,
                "duration_ms": round(self.duration_ms, 1)}


class BaseAetherPlugin(ABC):
    """Subclass, set ``name``/``description`` and implement :meth:`run`."""

    name: str = ""
    description: str = ""
    version: str = "1.0.0"

    def __init__(self, kernel: AetherKernel) -> None:
        self.kernel = kernel

    def is_applicable(self) -> bool:
        """Return False to skip quietly (e.g. no Dockerfiles in the project)."""
        return True

    @abstractmethod
    def run(self) -> dict[str, Any]:
        """Execute the plugin and return a JSON-serializable summary."""

    # Helpers available to every plugin --------------------------------------------------------
    def repo_roots(self) -> list[tuple[str, Path]]:
        roots = [(self.kernel.repo_name, self.kernel.root)]
        for link in self.kernel.config.linked_repos:
            p = Path(link["path"])
            if p.is_dir():
                roots.append((link["name"], p.resolve()))
        return roots


def audit_plugin_source(source: str) -> list[str]:
    """Return a list of policy violations found in plugin source code."""
    problems: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in FORBIDDEN_MODULES:
                    problems.append(f"line {node.lineno}: import of forbidden module '{a.name}'")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in FORBIDDEN_MODULES:
                problems.append(f"line {node.lineno}: import from forbidden module '{node.module}'")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in FORBIDDEN_CALLS:
                problems.append(f"line {node.lineno}: call to forbidden builtin '{fn.id}'")
            if isinstance(fn, ast.Attribute) and fn.attr in FORBIDDEN_ATTRS:
                problems.append(f"line {node.lineno}: call to forbidden attribute '.{fn.attr}'")
    return problems


def _plugin_classes(module: ModuleType) -> list[type[BaseAetherPlugin]]:
    out = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, BaseAetherPlugin) and obj is not BaseAetherPlugin and not inspect.isabstract(obj) and obj.__module__ == module.__name__:
            if not obj.name or not obj.name.replace("_", "").isalnum():
                continue
            out.append(obj)
    return out


class PluginManager:
    def __init__(self, kernel: AetherKernel) -> None:
        self.kernel = kernel
        self.load_errors: list[str] = []
        self.plugins: list[BaseAetherPlugin] = self._discover()

    def _discover(self) -> list[BaseAetherPlugin]:
        found: dict[str, BaseAetherPlugin] = {}
        import plugins as pkg

        for info in pkgutil.iter_modules(pkg.__path__):
            if info.name in {"base", "__init__"}:
                continue
            try:
                mod = importlib.import_module(f"plugins.{info.name}")
            except Exception as exc:  # noqa: BLE001
                self.load_errors.append(f"plugins.{info.name}: {exc}")
                continue
            for cls in _plugin_classes(mod):
                found[cls.name] = cls(self.kernel)

        local_dir = self.kernel.aether_dir / "plugins"
        trusted = set(self.kernel.config.trusted_plugins)
        if local_dir.is_dir():
            for path in sorted(local_dir.glob("*.py")):
                if path.stem not in trusted:
                    self.load_errors.append(f"{path.name}: skipped (not listed in trusted_plugins)")
                    continue
                problems = audit_plugin_source(path.read_text(encoding="utf-8"))
                if problems:
                    self.load_errors.append(f"{path.name}: rejected by audit: {'; '.join(problems[:5])}")
                    continue
                try:
                    spec = importlib.util.spec_from_file_location(f"aether_local_plugin_{path.stem}", path)
                    if spec is None or spec.loader is None:
                        raise ImportError("cannot create module spec")
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                except Exception as exc:  # noqa: BLE001
                    self.load_errors.append(f"{path.name}: {exc}")
                    continue
                for cls in _plugin_classes(mod):
                    if cls.name in found:
                        self.load_errors.append(f"{path.name}: name '{cls.name}' collides with a built-in plugin")
                        continue
                    found[cls.name] = cls(self.kernel)
        disabled = set(self.kernel.config.disabled_plugins)
        return [p for name, p in sorted(found.items()) if name not in disabled]

    def describe(self) -> list[dict[str, Any]]:
        return [{"name": p.name, "description": p.description, "version": p.version, "module": type(p).__module__}
                for p in self.plugins]

    def run(self, plugin: BaseAetherPlugin, timeout: float = PLUGIN_TIMEOUT_S) -> PluginResult:
        t0 = time.perf_counter()
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                if not plugin.is_applicable():
                    box["summary"] = {"skipped": True, "reason": "not applicable"}
                else:
                    box["summary"] = plugin.run()
            except Exception as exc:  # noqa: BLE001
                box["error"] = f"{exc.__class__.__name__}: {exc}"

        th = threading.Thread(target=target, name=f"aether-plugin-{plugin.name}", daemon=True)
        th.start()
        th.join(timeout)
        dur = (time.perf_counter() - t0) * 1000
        if th.is_alive():
            return PluginResult(plugin.name, False, {}, f"timed out after {timeout:.0f}s", dur)
        if "error" in box:
            return PluginResult(plugin.name, False, {}, box["error"], dur)
        result = PluginResult(plugin.name, True, box.get("summary", {}), "", dur)
        self.kernel.bus.publish("PLUGIN_RUN", {"plugin": plugin.name, "ok": True, "duration_ms": round(dur, 1)})
        return result

    def run_all(self) -> list[PluginResult]:
        return [self.run(p) for p in self.plugins]

    def run_named(self, name: str) -> PluginResult:
        for p in self.plugins:
            if p.name == name:
                return self.run(p)
        return PluginResult(name, False, {}, f"plugin '{name}' not found")
