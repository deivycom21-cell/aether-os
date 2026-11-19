"""One-shot onboarding helpers behind ``aether setup``.

* :func:`check_prerequisites` reports Python / git availability with a concrete fix for each gap.
* :func:`looks_like_project` stops ``setup`` from indexing a home folder or a drive root by accident.
* :func:`detect_clients` finds which agentic IDEs are installed (CLI on PATH or their config folders),
  so the MCP server is registered only where it will be used.
* :func:`verify_mcp_server` spawns the exact command written into the IDE configs and performs a real
  MCP ``initialize`` + ``tools/list`` handshake over stdio, proving the install works end to end.

Stdlib only; nothing here talks to the network.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aether_core import __version__
from aether_core.mcp_install import DEFAULT_TARGETS, TARGETS

PROJECT_MARKERS = (
    ".git", ".aether", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile",
    "package.json", "deno.json", "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
    "composer.json", "Gemfile", "mix.exs", "pubspec.yaml", "CMakeLists.txt", "Makefile",
)

INSTALL_HINTS = {
    "git": {
        "win32": "winget install --id Git.Git -e --source winget",
        "darwin": "xcode-select --install   (or: brew install git)",
        "linux": "sudo apt install git   (or your distro's package manager)",
    },
}


@dataclass(frozen=True, slots=True)
class ClientProbe:
    key: str
    executables: tuple[str, ...]
    home_entries: tuple[str, ...]
    config_dirs: tuple[str, ...]


PROBES: tuple[ClientProbe, ...] = (
    ClientProbe("claude", ("claude",), (".claude", ".claude.json"), ()),
    ClientProbe("cursor", ("cursor",), (".cursor",), ("Cursor",)),
    ClientProbe("windsurf", ("windsurf",), (".codeium/windsurf", ".windsurf"), ("Windsurf",)),
    ClientProbe("vscode", ("code",), (".vscode",), ("Code",)),
    ClientProbe("zed", ("zed",), (), ("Zed", "zed")),
    ClientProbe("opencode", ("opencode",), (".opencode",), ("opencode",)),
)


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    return "darwin" if sys.platform == "darwin" else "linux"


def check_prerequisites(which: Callable[[str], str | None] = shutil.which) -> list[dict[str, Any]]:
    """Python >= 3.10 (hard requirement) and git (needed by hooks, hotspots churn, semantic diff, swarm)."""
    py_ok = sys.version_info >= (3, 10)
    git_path = which("git")
    return [
        {"name": "python", "ok": py_ok, "required": True,
         "detail": f"{sys.version.split()[0]} ({sys.executable})",
         "fix": "" if py_ok else "install Python 3.10+ (uv python install 3.12)"},
        {"name": "git", "ok": git_path is not None, "required": False,
         "detail": git_path or "not found on PATH",
         "fix": "" if git_path else INSTALL_HINTS["git"][_platform_key()]},
    ]


def looks_like_project(path: Path) -> bool:
    """True when ``path`` is a plausible project root (never the home folder or a filesystem root)."""
    path = path.resolve()
    if path == Path.home().resolve() or path.parent == path:
        return False
    return any((path / marker).exists() for marker in PROJECT_MARKERS) or any(path.glob("*.sln"))


def _config_roots(home: Path, env: Mapping[str, str]) -> list[Path]:
    roots = [Path(env[k]) for k in ("APPDATA", "LOCALAPPDATA") if env.get(k)]
    roots.append(Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config")
    roots.append(home / "Library" / "Application Support")
    return roots


def detect_clients(home: Path | None = None, env: Mapping[str, str] | None = None,
                   which: Callable[[str], str | None] = shutil.which) -> list[dict[str, Any]]:
    """Which supported IDEs look installed on this machine, with the evidence for each."""
    home = home or Path.home()
    env = os.environ if env is None else env
    roots = _config_roots(home, env)
    out: list[dict[str, Any]] = []
    for probe in PROBES:
        evidence = [f"{exe} on PATH" for exe in probe.executables if which(exe)]
        evidence += [f"~/{rel}" for rel in probe.home_entries if (home / Path(rel)).exists()]
        evidence += [str(root / name) for root in roots for name in probe.config_dirs if (root / name).is_dir()]
        out.append({"key": probe.key, "label": TARGETS[probe.key].label, "detected": bool(evidence),
                    "evidence": list(dict.fromkeys(evidence))[:3]})
    return out


def choose_targets(detected: list[dict[str, Any]]) -> list[str]:
    """Every detected IDE; Claude Code + Cursor when nothing is detected (their configs are harmless)."""
    keys = [d["key"] for d in detected if d["detected"]]
    return keys or list(DEFAULT_TARGETS)


def is_ephemeral_interpreter(executable: str | None = None) -> bool:
    """``uvx``/cache interpreters disappear on cache cleanup, so IDE configs must not point at them."""
    parts = {p.lower() for p in Path(executable or sys.executable).parts}
    return "archive-v0" in parts or ("cache" in parts and "uv" in parts and "tools" not in parts)


def _send(proc: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
    proc.stdin.flush()


def _await_response(lines: queue.Queue[bytes | None], msg_id: int, deadline: float) -> dict[str, Any] | None:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            raw = lines.get(timeout=remaining)
        except queue.Empty:
            return None
        if raw is None:
            return None
        try:
            msg = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and msg.get("id") == msg_id:
            return msg


def _pump(stream: Any, sink: queue.Queue[bytes | None]) -> None:
    try:
        for line in iter(stream.readline, b""):
            sink.put(line)
    except (OSError, ValueError):
        pass
    finally:
        sink.put(None)


def _drain(stream: Any, tail: list[bytes]) -> None:
    try:
        for line in iter(stream.readline, b""):
            tail.append(line)
            del tail[:-20]
    except (OSError, ValueError):
        pass


def verify_mcp_server(command: str, args: list[str], timeout: float = 180.0, cwd: Path | None = None) -> dict[str, Any]:
    """Launch the MCP server exactly as an IDE would and complete ``initialize`` + ``tools/list``."""
    started = time.monotonic()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen([command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=str(cwd) if cwd else None, env=env, creationflags=flags)
    except OSError as exc:
        return {"ok": False, "error": f"could not start the server: {exc}", "tools": 0, "tool_names": [], "ms": 0}
    lines: queue.Queue[bytes | None] = queue.Queue()
    stderr_tail: list[bytes] = []
    threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True).start()
    threading.Thread(target=_drain, args=(proc.stderr, stderr_tail), daemon=True).start()
    deadline = started + timeout
    result: dict[str, Any] = {"ok": False, "tools": 0, "tool_names": [], "server": None}
    try:
        protocol = _protocol_version()
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": protocol, "capabilities": {},
                                "clientInfo": {"name": "aether-setup", "version": __version__}}})
        init = _await_response(lines, 1, deadline)
        if init is None or "result" not in init:
            result["error"] = _failure("initialize", init, proc, stderr_tail)
            return result
        result["server"] = init["result"].get("serverInfo")
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = _await_response(lines, 2, deadline)
        if listed is None or "result" not in listed:
            result["error"] = _failure("tools/list", listed, proc, stderr_tail)
            return result
        names = sorted(t.get("name", "") for t in listed["result"].get("tools", []))
        result.update(ok=bool(names), tools=len(names), tool_names=names)
        if not names:
            result["error"] = "server answered but exposes no tools"
        return result
    except OSError as exc:
        result["error"] = f"server pipe closed: {exc}"
        return result
    finally:
        result["ms"] = int((time.monotonic() - started) * 1000)
        _stop(proc)


def _protocol_version() -> str:
    try:
        from mcp.types import LATEST_PROTOCOL_VERSION
    except ImportError:
        return "2025-06-18"
    return str(LATEST_PROTOCOL_VERSION)


def _failure(step: str, msg: dict[str, Any] | None, proc: subprocess.Popen[bytes], stderr_tail: list[bytes]) -> str:
    if msg is not None and "error" in msg:
        return f"{step} rejected: {msg['error']}"
    try:
        code = proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        code = None
    tail = b"".join(stderr_tail[-6:]).decode("utf-8", errors="replace").strip()
    state = f"server exited with code {code}" if code is not None else "no answer before the timeout"
    return f"{step}: {state}" + (f"\n{tail}" if tail else "")


def _stop(proc: subprocess.Popen[bytes]) -> None:
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return
