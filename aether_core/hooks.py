"""Install / remove the AetherOS git pre-commit hook.

The hook is a POSIX ``sh`` script: Git for Windows runs hooks through its bundled ``sh.exe``, so the
same file works on Windows, macOS and Linux. For speed it runs the *base* CPython interpreter with
``-S`` (skips ``site``, .pth files and the venv launcher) and ``PYTHONPATH`` set to the directory that
contains ``aether_core`` (the scan needs only the standard library). It chains to a pre-existing hook,
which is preserved as ``pre-commit.local``.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

HOOK_MARKER = "# AETHER-PRE-COMMIT-HOOK"
LOCAL_HOOK = "pre-commit.local"


def hooks_dir(root: Path) -> Path:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git executable not found on PATH")
    proc = subprocess.run([git, "-C", str(root), "rev-parse", "--git-path", "hooks"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"{root} is not a git repository")
    p = Path(proc.stdout.strip())
    return p if p.is_absolute() else (root / p).resolve()


def fast_interpreter() -> str:
    """The real interpreter behind a venv (avoids the ~100 ms venv launcher re-exec on Windows)."""
    base = getattr(sys, "_base_executable", "") or sys.executable
    return base if Path(base).is_file() else sys.executable


def package_parent() -> str:
    import aether_core

    return Path(aether_core.__file__).resolve().parent.parent.as_posix()


def hook_script(python: str, pythonpath: str) -> str:
    py = Path(python).resolve().as_posix()
    return f"""#!/bin/sh
{HOOK_MARKER} (installed by `aether install-hooks`; remove with `aether install-hooks --uninstall`)
# Scans only the lines added in this commit for secrets and prompt injections.
# Bypass once: git commit --no-verify   (or AETHER_HOOK_SKIP=1)
PYTHONPATH="{pythonpath}" "{py}" -S -m aether_core.precommit "$@" || exit $?
HOOK_DIR=$(dirname "$0")
if [ -x "$HOOK_DIR/{LOCAL_HOOK}" ]; then
  exec "$HOOK_DIR/{LOCAL_HOOK}" "$@"
fi
exit 0
"""


def install_hook(root: Path, python: str | None = None) -> dict[str, Any]:
    hd = hooks_dir(root)
    hd.mkdir(parents=True, exist_ok=True)
    target = hd / "pre-commit"
    chained = False
    if target.is_file():
        current = target.read_text(encoding="utf-8", errors="replace")
        if HOOK_MARKER not in current:
            local = hd / LOCAL_HOOK
            if local.exists():
                raise RuntimeError(f"both {target} (foreign) and {local} exist; resolve manually")
            os.replace(target, local)
            chained = True
    script = hook_script(python or fast_interpreter(), package_parent())
    target.write_bytes(script.encode("utf-8"))  # LF endings for sh
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return {"ok": True, "path": str(target), "chained_existing_hook": chained or (hd / LOCAL_HOOK).exists()}


def uninstall_hook(root: Path) -> dict[str, Any]:
    hd = hooks_dir(root)
    target = hd / "pre-commit"
    if not target.is_file() or HOOK_MARKER not in target.read_text(encoding="utf-8", errors="replace"):
        return {"ok": False, "error": "no AetherOS pre-commit hook installed", "path": str(target)}
    target.unlink()
    local = hd / LOCAL_HOOK
    restored = False
    if local.is_file():
        os.replace(local, target)
        restored = True
    return {"ok": True, "path": str(target), "restored_previous_hook": restored}


def hook_status(root: Path) -> dict[str, Any]:
    hd = hooks_dir(root)
    target = hd / "pre-commit"
    installed = target.is_file() and HOOK_MARKER in target.read_text(encoding="utf-8", errors="replace")
    return {"installed": installed, "path": str(target), "chained": (hd / LOCAL_HOOK).is_file()}
