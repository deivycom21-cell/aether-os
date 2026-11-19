"""Env shield: audits ``.env`` files against ``.env.example`` without ever exposing values.

Reports undocumented keys, unset keys, real-looking secrets leaked into the example file and
``.env`` files that are not git-ignored. Only variable *names* are added to the graph.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from aether_core.security import detect_secrets
from plugins.base import BaseAetherPlugin

EXAMPLE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.\-]*)\s*=\s*(.*)$")
SENSITIVE_NAME = re.compile(r"(?i)(key|secret|token|password|passwd|pwd|credential|private|auth|dsn|cookie|salt)")


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        value = m.group(2).strip()
        if value and value[0] in "\"'" and value.endswith(value[0]) and len(value) >= 2:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        out[m.group(1)] = value
    return out


def is_example(name: str) -> bool:
    return name.lower().endswith(EXAMPLE_SUFFIXES)


def git_ignored(root: Path, rel: str) -> bool | None:
    git = shutil.which("git")
    if not git or not (root / ".git").exists():
        return None
    try:
        proc = subprocess.run([git, "-C", str(root), "check-ignore", "-q", rel], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0


class EnvShield(BaseAetherPlugin):
    name = "env_shield"
    description = "Audits .env vs .env.example, flags leaked secrets and un-ignored env files (names only)."

    def _env_files(self, root: Path) -> list[Path]:
        return sorted(p for p in root.glob(".env*") if p.is_file())

    def is_applicable(self) -> bool:
        return any(self._env_files(root) for _, root in self.repo_roots())

    def run(self) -> dict[str, Any]:
        db = self.kernel.db
        reports: list[dict[str, Any]] = []
        for repo_name, root in self.repo_roots():
            files = self._env_files(root)
            if not files:
                continue
            examples = [f for f in files if is_example(f.name)]
            reals = [f for f in files if not is_example(f.name)]
            example_vars: dict[str, str] = {}
            for ex in examples:
                example_vars.update(parse_env(ex.read_text(encoding="utf-8", errors="replace")))
            real_keys: set[str] = set()
            unignored: list[str] = []
            for rf in reals:
                real_keys.update(parse_env(rf.read_text(encoding="utf-8", errors="replace")).keys())
                ignored = git_ignored(root, rf.name)
                if ignored is False:
                    unignored.append(rf.name)
                    db.add_security_finding("env", "env_file_not_gitignored", "critical", rf.name, 0,
                                            "Real .env file is tracked or not ignored by git")
            leaks: list[dict[str, Any]] = []
            for ex in examples:
                text = ex.read_text(encoding="utf-8", errors="replace")
                for f in detect_secrets(text):
                    key_line = text.splitlines()[f.line - 1] if f.line - 1 < len(text.splitlines()) else ""
                    m = _LINE.match(key_line)
                    leaks.append({"file": ex.name, "line": f.line, "key": m.group(1) if m else "", "rule": f.rule})
                    db.add_security_finding("env", f"example_leak:{f.rule}", "critical", ex.name, f.line,
                                            "Real-looking secret committed in example env file")
            example_keys = set(example_vars)
            report = {
                "repo": repo_name,
                "env_files": [f.name for f in reals],
                "example_files": [f.name for f in examples],
                "undocumented_keys": sorted(real_keys - example_keys) if examples else sorted(real_keys),
                "unset_keys": sorted(example_keys - real_keys) if reals else [],
                "sensitive_keys": sorted(k for k in real_keys | example_keys if SENSITIVE_NAME.search(k)),
                "example_leaks": leaks,
                "not_gitignored": unignored,
                "status": "critical" if leaks or unignored else "warning" if (real_keys - example_keys and examples) or not examples else "ok",
            }
            reports.append(report)
            # Register env var *names* on the example file (never values) so agents can discover config.
            if examples:
                ex = examples[0]
                repo_id = db.upsert_repo(repo_name, root, repo_name == self.kernel.repo_name)
                raw = ex.read_bytes()
                nodes = [{
                    "kind": "module", "name": ex.name, "qualname": ex.name, "signature": f"{len(example_keys)} variables",
                    "docstring": "Environment variable template (names only)", "body": "", "start_line": 1, "end_line": 1,
                    "metadata": {"language": "env"},
                }]
                for k in sorted(example_keys | real_keys):
                    nodes.append({
                        "kind": "env_var", "name": k, "qualname": f"env:{k}",
                        "signature": f"{k}=<{'secret' if SENSITIVE_NAME.search(k) else 'value'}>",
                        "docstring": ("documented" if k in example_keys else "UNDOCUMENTED (missing from example)"),
                        "body": "", "start_line": 1, "end_line": 1,
                        "metadata": {"sensitive": bool(SENSITIVE_NAME.search(k)), "documented": k in example_keys, "set": k in real_keys},
                    })
                db.replace_file(repo_id, ex.name, "env", hashlib.sha1(raw).hexdigest(), len(raw),
                                raw.count(b"\n") + 1, 0, nodes, [])
        return {"reports": reports, "critical": sum(1 for r in reports if r["status"] == "critical")}
