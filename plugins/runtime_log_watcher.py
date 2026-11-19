"""Runtime log watcher: mines local ``*.log`` files for exceptions that keep recurring and turns each
recurring failure into a preventive scar on the project file (and function) where it originates.

Messages are normalised (numbers, ids, hex, quoted values and paths collapsed) so "user 42 not found"
and "user 97 not found" count as the same failure, and everything is secret-masked before storage.
Only the tail of each log is read (``MAX_LOG_BYTES``) to stay fast on huge files.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from aether_core.ast_indexer import DEFAULT_IGNORE_DIRS
from plugins.base import BaseAetherPlugin

MAX_LOG_FILES = 60
MAX_LOG_BYTES = 2 * 1024 * 1024
MIN_OCCURRENCES = 2
LOG_SUFFIXES = (".log", ".log.txt", ".out", ".err")
_PY_FRAME = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>[\w<>.]+)')
_PY_EXC = re.compile(r"^(?P<etype>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)|[A-Z]\w+Error)(?::\s*(?P<msg>.*))?$")
_JS_EXC = re.compile(r"^\s*(?:Uncaught\s+)?(?P<etype>(?:[A-Z]\w*)?Error|UnhandledPromiseRejection\w*)(?::\s*(?P<msg>.*))?$")
_JS_FRAME = re.compile(r"^\s+at\s+(?:(?P<func>[^\s(]+)\s+)?\(?(?P<path>(?:[A-Za-z]:)?[^():\s]+\.(?:[cm]?[jt]sx?)):(?P<line>\d+):\d+\)?")
_LEVEL_LINE = re.compile(r"\b(?P<level>ERROR|CRITICAL|FATAL|SEVERE|PANIC)\b[\]:\s-]*(?P<msg>.+)$")
_NORMALISE = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hash>"),
    (re.compile(r"(?:[A-Za-z]:)?(?:[\\/][\w.\-]+){2,}"), "<path>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<value>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<n>"),
    (re.compile(r"\s+"), " "),
)
_TIMESTAMP = re.compile(r"^\s*\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?\s*")


def normalise_message(msg: str) -> str:
    out = msg.strip()
    for rx, repl in _NORMALISE:
        out = rx.sub(repl, out)
    return out.strip()[:240]


@dataclass(slots=True)
class Failure:
    etype: str
    message: str
    frames: list[tuple[str, int, str]] = field(default_factory=list)


def parse_log(text: str) -> list[Failure]:
    """Extract exceptions (Python tracebacks, Node stacks, ERROR/FATAL level lines) in order."""
    failures: list[Failure] = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = _TIMESTAMP.sub("", lines[i])
        if line.startswith("Traceback (most recent call last)") or "Traceback (most recent call last)" in line:
            frames: list[tuple[str, int, str]] = []
            j = i + 1
            while j < n:
                fm = _PY_FRAME.search(lines[j])
                if fm:
                    frames.append((fm.group("path"), int(fm.group("line")), fm.group("func")))
                    j += 1
                    continue
                if lines[j].startswith((" ", "\t")) or not lines[j].strip():
                    j += 1
                    continue
                em = _PY_EXC.match(lines[j].strip())
                if em:
                    failures.append(Failure(em.group("etype"), em.group("msg") or "", frames))
                break
            i = j + 1
            continue
        jm = _JS_EXC.match(line)
        if jm and i + 1 < n and _JS_FRAME.match(lines[i + 1]):
            frames = []
            j = i + 1
            while j < n and (fm := _JS_FRAME.match(lines[j])):
                frames.append((fm.group("path"), int(fm.group("line")), fm.group("func") or ""))
                j += 1
            failures.append(Failure(jm.group("etype"), jm.group("msg") or "", frames))
            i = j
            continue
        lm = _LEVEL_LINE.search(line)
        if lm and "Traceback" not in line:
            msg = lm.group("msg").strip()
            em = re.search(r"([A-Z]\w*(?:Error|Exception))(?::\s*(.*))?", msg)
            failures.append(Failure(em.group(1) if em else lm.group("level"), (em.group(2) or "") if em else msg, []))
        i += 1
    return failures


class RuntimeLogWatcher(BaseAetherPlugin):
    name = "runtime_log_watcher"
    description = "Turns exceptions that repeat in local *.log files into preventive scars on the originating code."

    def _logs(self, root: Path) -> list[Path]:
        out: list[Path] = []
        stack = [root]
        while stack and len(out) < MAX_LOG_FILES:
            cur = stack.pop()
            try:
                entries = sorted(cur.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for e in entries:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    if e.name not in DEFAULT_IGNORE_DIRS and not e.name.endswith(".egg-info"):
                        stack.append(e)
                elif e.name.lower().endswith(LOG_SUFFIXES):
                    out.append(e)
        return out

    def is_applicable(self) -> bool:
        return any(self._logs(root) for _, root in self.repo_roots())

    @staticmethod
    def _project_frame(frames: list[tuple[str, int, str]], root: Path) -> tuple[str, int, str] | None:
        for path, line, func in reversed(frames):  # innermost first
            if re.search(r"site-packages|node_modules|[\\/]lib[\\/]python|<frozen|<string>", path):
                continue
            p = Path(path)
            try:
                rel = p.resolve().relative_to(root.resolve()).as_posix() if p.is_absolute() else PurePosixPath(path.replace("\\", "/")).as_posix()
            except (ValueError, OSError):
                continue
            if (root / rel).is_file():
                return rel, line, func
        return None

    def run(self) -> dict[str, Any]:
        db = self.kernel.db
        vault = self.kernel.vault
        scars_created = 0
        recurring: list[dict[str, Any]] = []
        logs_read = 0
        for repo_name, root in self.repo_roots():
            groups: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(lambda: {"count": 0, "example": "", "logs": set()})
            for log in self._logs(root):
                try:
                    size = log.stat().st_size
                    with log.open("rb") as fh:
                        if size > MAX_LOG_BYTES:
                            fh.seek(size - MAX_LOG_BYTES)
                        text = fh.read().decode("utf-8", errors="replace")
                except OSError:
                    continue
                logs_read += 1
                log_rel = log.relative_to(root).as_posix()
                for f in parse_log(text):
                    origin = self._project_frame(f.frames, root)
                    file_rel, _, func = origin if origin else (log_rel, 0, "")
                    key = (f.etype, normalise_message(f.message), f"{file_rel}::{func}")
                    g = groups[key]
                    g["count"] += 1
                    g["example"] = f.message.strip()[:300]
                    g["logs"].add(log_rel)
                    g["file"], g["func"] = file_rel, func
            for (etype, norm, _), g in groups.items():
                if g["count"] < MIN_OCCURRENCES:
                    continue
                example = vault.mask(g["example"]).text
                summary = vault.mask(f"Recurring runtime error: {etype}: {norm}").text[:300]
                severity = "high" if g["count"] >= 20 else "medium" if g["count"] >= 5 else "low"
                logs = ", ".join(sorted(g["logs"]))[:200]
                _, created = db.add_scar(
                    file_path=g["file"], symbol=g["func"], error_summary=summary,
                    fix_applied=f"Unresolved: seen {g['count']}x in {logs}. Add handling/tests before changing this code.",
                    severity=severity, root_cause=f"Mined from local runtime logs. Last message: {example}",
                    source="runtime_log", repo_name=repo_name,
                )
                scars_created += int(created)
                recurring.append({"repo": repo_name, "file": g["file"], "symbol": g["func"], "error": summary.split(": ", 1)[-1],
                                  "count": g["count"], "severity": severity, "logs": sorted(g["logs"])})
        recurring.sort(key=lambda r: -r["count"])
        if recurring:
            self.kernel.bus.publish("SCAR_RECORDED", {"file": recurring[0]["file"], "severity": recurring[0]["severity"],
                                                      "created": scars_created > 0, "source": "runtime_log"},
                                    [f"{r['repo']}:{r['file']}" for r in recurring[:20]])
        return {"logs_read": logs_read, "recurring_errors": recurring[:50], "scars_created": scars_created}
