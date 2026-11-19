"""Episodic session memory: compact checkpoints (goal, touched files, pending work, decisions)
that let an agent resume a task in a fresh context window for < 400 tokens."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from aether_core.context_engine import estimate_tokens

MAX_CHECKPOINT_TOKENS = 400


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def git_touched_files(root: Path, limit: int = 40) -> list[str]:
    """Files modified, staged or untracked in the working tree (``git status --porcelain``)."""
    git = shutil.which("git")
    if not git or not (root / ".git").exists():
        return []
    try:
        proc = subprocess.run([git, "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"').replace("\\", "/")
        if path.startswith(".aether/") or path.endswith("/"):
            continue
        out.append(path)
    return out[:limit]


def render_summary(goal: str, files: list[str], pending: list[str], decisions: list[str], notes: str) -> str:
    lines = [f"GOAL: {goal}"]
    if files:
        lines.append("FILES: " + ", ".join(files))
    if pending:
        lines.append("PENDING:")
        lines.extend(f"- {p}" for p in pending)
    if decisions:
        lines.append("DECISIONS:")
        lines.extend(f"- {d}" for d in decisions)
    if notes:
        lines.append(f"NOTES: {notes}")
    return "\n".join(lines)


def build_checkpoint(goal: str, files: list[str], pending: list[str], decisions: list[str], notes: str,
                     max_tokens: int = MAX_CHECKPOINT_TOKENS) -> tuple[str, int, dict[str, list[str] | str]]:
    """Return (summary, tokens, kept_fields) shrinking lists/text until the summary fits ``max_tokens``.

    Shrinking order keeps what matters most for resuming: the goal, then pending work, then decisions,
    then files, then free-form notes.
    """
    goal = _clip(goal, 300)
    f = [_clip(x, 120) for x in files if x.strip()][:25]
    p = [_clip(x, 200) for x in pending if x.strip()][:12]
    d = [_clip(x, 200) for x in decisions if x.strip()][:10]
    n = _clip(notes, 600)
    summary = render_summary(goal, f, p, d, n)
    tokens = estimate_tokens(summary)
    steps = 0
    while tokens > max_tokens and steps < 400:
        steps += 1
        if len(n) > 80:
            n = _clip(n, max(80, int(len(n) * 0.7)))
        elif len(f) > 6:
            f = f[: len(f) - 1]
        elif len(d) > 3:
            d = d[: len(d) - 1]
        elif len(p) > 4:
            p = p[: len(p) - 1]
        elif any(len(x) > 80 for x in p + d):
            p = [_clip(x, 80) for x in p]
            d = [_clip(x, 80) for x in d]
        elif n:
            n = ""
        elif f:
            f = f[: len(f) - 1]
        elif d:
            d = d[: len(d) - 1]
        elif len(p) > 1:
            p = p[: len(p) - 1]
        else:
            goal = _clip(goal, max(40, int(len(goal) * 0.7)))
        summary = render_summary(goal, f, p, d, n)
        tokens = estimate_tokens(summary)
    return summary, tokens, {"goal": goal, "files": f, "pending": p, "decisions": d, "notes": n}
