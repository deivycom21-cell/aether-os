"""Ultra-fast pre-commit guard: scans only the lines being *added* in the staged diff for secrets and
prompt-injection payloads, so it stays well under 150 ms on typical commits.

Invoked by the hook that ``aether install-hooks`` writes and by ``aether scan --staged``. The hook runs
the *base* interpreter with ``-S`` (no ``site``/venv launcher, no .pth processing) and ``PYTHONPATH``
pointing at this package, and this module imports nothing heavier than the security module: no Typer,
Rich, SQLite, JSON or kernel start-up on the commit path. The scan itself (one ``git diff`` + regex and
entropy checks) typically takes ~30 ms; the rest of the wall time is CPython start-up.

Bypass for one commit: ``git commit --no-verify`` or ``AETHER_HOOK_SKIP=1``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aether_core.security import PromptInjectionDetector, detect_secrets

BLOCKING_INJECTION = frozenset({"high", "critical"})
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(slots=True)
class StagedFile:
    path: str
    lines: list[tuple[int, str]] = field(default_factory=list)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    # "git" is resolved through PATH by CreateProcess/execvp; avoiding shutil keeps bz2/lzma off the commit path.
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=60)


def staged_additions(root: Path) -> list[StagedFile]:
    """Parse ``git diff --cached -U0`` into added lines per file (binary files are skipped by git)."""
    proc = _git(root, "-c", "core.quotepath=off", "diff", "--cached", "-U0", "--no-color", "--no-ext-diff",
                "--diff-filter=ACMR", "--text")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or "git diff --cached failed")
    files: list[StagedFile] = []
    current: StagedFile | None = None
    lineno = 0
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                current = None
                continue
            if target.startswith('"') and target.endswith('"'):
                target = target[1:-1].replace('\\"', '"').replace("\\t", "\t").replace("\\\\", "\\")
            current = StagedFile(target[2:] if target.startswith("b/") else target)
            files.append(current)
            continue
        m = _HUNK.match(raw)
        if m:
            lineno = int(m.group(1))
            continue
        if current is not None and raw.startswith("+") and not raw.startswith("+++"):
            current.lines.append((lineno, raw[1:]))
            lineno += 1
    return [f for f in files if f.lines]


def load_scan_ignore(root: Path) -> list[str]:
    path = root / ".aether" / "policy.json"
    if not path.is_file():
        return []
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    value = data.get("scan_ignore", []) if isinstance(data, dict) else []
    return [str(v) for v in value] if isinstance(value, list) else []


def scan_staged(root: Path) -> dict[str, Any]:
    t0 = time.perf_counter()
    ignore = load_scan_ignore(root)
    if ignore:
        from aether_core.policy import Policy

        ignored = Policy(scan_ignore=ignore).scan_ignored
    else:
        def ignored(_: str) -> bool:
            return False
    detector = PromptInjectionDetector()
    secrets: list[dict[str, Any]] = []
    injections: list[dict[str, Any]] = []
    skipped: list[str] = []
    staged = staged_additions(root)
    for f in staged:
        if ignored(f.path):
            skipped.append(f.path)
            continue
        text = "\n".join(line for _, line in f.lines)
        for finding in detect_secrets(text):
            real_line = f.lines[min(len(f.lines), finding.line) - 1][0]
            secrets.append({"file": f.path, "line": real_line, "rule": finding.rule, "severity": finding.severity,
                            "preview": finding.preview})
        report = detector.analyze(text)
        if report.is_suspicious and report.severity in BLOCKING_INJECTION:
            first = report.findings[0].line if report.findings else 1
            injections.append({"file": f.path, "line": f.lines[min(len(f.lines), first) - 1][0],
                               "severity": report.severity, "score": round(report.score, 2),
                               "rules": sorted({x.rule for x in report.findings})})
    blocked = bool(secrets or injections)
    return {"ok": not blocked, "blocked": blocked, "files_scanned": len(staged) - len(skipped), "skipped": skipped,
            "secrets": secrets, "injections": injections, "duration_ms": round((time.perf_counter() - t0) * 1000, 1)}


def format_report(r: dict[str, Any]) -> str:
    if r["ok"]:
        return f"aether: staged changes clean ({r['files_scanned']} file(s), {r['duration_ms']} ms)"
    lines = ["", "aether: COMMIT BLOCKED - staged changes contain sensitive material", ""]
    for s in r["secrets"]:
        lines.append(f"  secret     {s['file']}:{s['line']}  {s['rule']}  {s['preview']}")
    for i in r["injections"]:
        lines.append(f"  injection  {i['file']}:{i['line']}  [{i['severity']}] {', '.join(i['rules'])}")
    lines += ["", "  Remove the material (or move it to an ignored .env), then re-stage.",
              "  False positive? add the path to scan_ignore in .aether/policy.json, or bypass once with",
              "  `git commit --no-verify`.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("AETHER_HOOK_SKIP"):
        return 0
    try:
        if os.environ.get("GIT_INDEX_FILE") or os.environ.get("GIT_DIR"):
            root = Path.cwd()  # git runs hooks from the top of the working tree
        else:
            top = _git(Path.cwd(), "rev-parse", "--show-toplevel")
            root = Path(top.stdout.decode("utf-8", "replace").strip()) if top.returncode == 0 else Path.cwd()
        report = scan_staged(root)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"aether: pre-commit scan could not run ({exc}); allowing commit\n")
        return 0
    stream = sys.stdout if report["ok"] else sys.stderr
    stream.write(format_report(report) + "\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
