"""Git history scars: mines commits whose subject mentions fix/bug/revert/... and turns each touched
file into a preventive scar, so agents are warned about historically fragile code."""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from plugins.base import BaseAetherPlugin

FIX_PATTERN = re.compile(
    r"\b(fix(?:es|ed|ing)?|bug(?:fix)?|hotfix|revert(?:s|ed)?|regression|crash(?:es|ed)?|patch(?:ed)?|"
    r"security|vuln(?:erability)?|cve-\d{4}-\d+|broken|race condition|deadlock|leak)\b",
    re.I,
)
HIGH_WORDS = re.compile(r"\b(revert|crash|security|vuln|cve-|regression|hotfix|data loss|deadlock|race condition)", re.I)
LOW_WORDS = re.compile(r"\b(typo|lint|format|docs?|comment|style)\b", re.I)
MAX_FILES_PER_COMMIT = 20
RS, US = "\x1e", "\x1f"


def classify_severity(subject: str) -> str:
    if HIGH_WORDS.search(subject):
        return "high"
    if LOW_WORDS.search(subject):
        return "low"
    return "medium"


def read_fix_commits(root: Path, limit: int = 400) -> list[dict[str, Any]]:
    git = shutil.which("git")
    if not git or not (root / ".git").exists():
        return []
    try:
        proc = subprocess.run(
            [git, "-C", str(root), "log", "--no-merges", f"-n{limit}", "--name-only",
             f"--format={RS}%H{US}%s{US}%aI"],
            capture_output=True, timeout=60, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    commits = []
    for block in proc.stdout.split(RS):
        block = block.strip()
        if not block:
            continue
        header, _, files_part = block.partition("\n")
        parts = header.split(US)
        if len(parts) < 3:
            continue
        sha, subject, date = parts[0], parts[1], parts[2]
        if not FIX_PATTERN.search(subject):
            continue
        files = [f.strip() for f in files_part.splitlines() if f.strip()]
        commits.append({"sha": sha, "subject": subject, "date": date, "files": files})
    return commits


class GitHistoryScars(BaseAetherPlugin):
    name = "git_history_scars"
    description = "Creates preventive scars from commits mentioning fix/bug/revert/crash/security."

    def is_applicable(self) -> bool:
        return shutil.which("git") is not None and any((root / ".git").exists() for _, root in self.repo_roots())

    def run(self) -> dict[str, Any]:
        db = self.kernel.db
        created = 0
        examined = 0
        skipped_bulk = 0
        for repo_name, root in self.repo_roots():
            for c in read_fix_commits(root):
                examined += 1
                if len(c["files"]) > MAX_FILES_PER_COMMIT:
                    skipped_bulk += 1
                    continue
                try:
                    ts = datetime.fromisoformat(c["date"]).timestamp()
                except ValueError:
                    ts = None
                sev = classify_severity(c["subject"])
                for rel in c["files"]:
                    if not (root / rel).is_file():
                        continue  # the file no longer exists
                    _, is_new = db.add_scar(
                        file_path=rel,
                        error_summary=f"Historically fragile: {c['subject'][:200]}",
                        fix_applied=f"Commit {c['sha'][:10]} ({c['date'][:10]}): {c['subject'][:200]}",
                        severity=sev,
                        root_cause="Mined from git history (commit subject matched fix/bug/revert patterns).",
                        source="git_history",
                        commit_sha=c["sha"],
                        repo_name=repo_name,
                        created_at=ts,
                    )
                    created += int(is_new)
        return {"fix_commits": examined, "scars_created": created, "bulk_commits_skipped": skipped_bulk}
