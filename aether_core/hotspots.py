"""Technical-debt hotspots: a 0-100 ``risk_score`` per file combining

* complexity    (30 %) – approximate cyclomatic complexity of every function/method, plus the worst one;
* fan-in        (25 %) – distinct files that import/call/HTTP-call this file;
* churn         (25 %) – commits touching the file in the last N commits (``git log``);
* scars         (20 %) – recorded bugs weighted by severity (critical 4 · high 3 · medium 2 · low 1).

Each factor is log-scaled against the project maximum so one monster file does not flatten the rest.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aether_core.ast_indexer import is_virtual_path

if TYPE_CHECKING:
    from aether_core.database import AetherDB

WEIGHTS = {"complexity": 0.30, "fan_in": 0.25, "churn": 0.25, "scars": 0.20}
SCAR_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
CODE_LANGUAGES = frozenset({
    "python", "typescript", "javascript", "go", "rust", "java", "kotlin", "ruby", "php", "csharp", "vue", "svelte",
    "shell", "powershell",
})
FUNCTION_KINDS = ("function", "method")
_BRANCH = re.compile(r"\b(?:if|elif|for|while|case|catch|except|and|or|match)\b|&&|\|\||\?\?|(?<![?.])\?(?![.?:])")
_COMMENT_LINE = re.compile(r"^\s*(?:#|//|\*|/\*)")


def cyclomatic(body: str) -> int:
    """Approximate McCabe complexity from source text (1 + decision points), ignoring comment lines."""
    code = "\n".join(line for line in body.splitlines() if not _COMMENT_LINE.match(line))
    code = re.sub(r"(\"\"\"|''')[\s\S]*?\1", "", code)
    code = re.sub(r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'", "\"\"", code)
    return 1 + len(_BRANCH.findall(code))


def git_churn(root: Path, commits: int = 500) -> Counter[str]:
    git = shutil.which("git")
    if not git or not (root / ".git").exists():
        return Counter()
    try:
        proc = subprocess.run([git, "-C", str(root), "log", f"-n{int(commits)}", "--no-merges", "--format=", "--name-only"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return Counter()
    if proc.returncode != 0:
        return Counter()
    return Counter(line.strip() for line in proc.stdout.splitlines() if line.strip())


def git_head(root: Path) -> str:
    git = shutil.which("git")
    if not git or not (root / ".git").exists():
        return ""
    try:
        proc = subprocess.run([git, "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


@dataclass(slots=True)
class Hotspot:
    repo: str
    file: str
    language: str
    risk_score: float
    level: str
    complexity: int
    max_function_complexity: int
    worst_function: str
    functions: int
    fan_in: int
    churn: int
    scars: int
    lines: int
    factors: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo, "file": self.file, "language": self.language, "risk_score": self.risk_score,
            "level": self.level, "complexity": self.complexity, "max_function_complexity": self.max_function_complexity,
            "worst_function": self.worst_function, "functions": self.functions, "fan_in": self.fan_in,
            "churn": self.churn, "scars": self.scars, "lines": self.lines, "factors": self.factors,
            "reasons": self.reasons,
        }


def _level(score: float) -> str:
    return "critical" if score >= 70 else "high" if score >= 50 else "medium" if score >= 25 else "low"


def _norm(value: float, maximum: float) -> float:
    return math.log1p(value) / math.log1p(maximum) if maximum > 0 and value > 0 else 0.0


def compute_hotspots(db: AetherDB, repo_roots: dict[str, Path], commits: int = 500) -> list[Hotspot]:
    files = [f for f in db.list_files() if f.language in CODE_LANGUAGES and not is_virtual_path(f.path)]
    if not files:
        return []
    complexity: dict[int, tuple[int, int, str, int]] = {}
    rows = db.query(
        "SELECT n.file_id, n.qualname, n.body FROM nodes n WHERE n.kind IN ('function', 'method')"
    )
    acc: dict[int, list[tuple[int, str]]] = {}
    for r in rows:
        acc.setdefault(int(r["file_id"]), []).append((cyclomatic(r["body"] or ""), str(r["qualname"])))
    for fid, items in acc.items():
        worst = max(items, key=lambda t: t[0])
        complexity[fid] = (sum(c for c, _ in items), worst[0], worst[1], len(items))
    fan_in = {int(r["dst"]): int(r["n"]) for r in db.query(
        "SELECT dst_file_id AS dst, COUNT(DISTINCT src_file_id) AS n FROM edges "
        "WHERE dst_file_id IS NOT NULL AND dst_file_id != src_file_id GROUP BY dst_file_id")}
    scar_weight: Counter[tuple[str, str]] = Counter()
    scar_count: Counter[tuple[str, str]] = Counter()
    for r in db.query("SELECT repo_name, file_path, severity FROM scars"):
        key = (str(r["repo_name"] or ""), str(r["file_path"]))
        scar_weight[key] += SCAR_WEIGHT.get(str(r["severity"]), 1)
        scar_count[key] += 1
    churn_by_repo = {name: git_churn(root, commits) for name, root in repo_roots.items()}

    raw: list[tuple[Any, float, int, int, int, int]] = []
    for f in files:
        total, worst, _, _ = complexity.get(f.id, (0, 0, "", 0))
        cx = total + 2 * worst  # the single worst function matters more than many trivial ones
        fi = fan_in.get(f.id, 0)
        ch = churn_by_repo.get(f.repo_name, Counter()).get(f.path, 0)
        sw = scar_weight.get((f.repo_name, f.path), 0) + scar_weight.get(("", f.path), 0)
        raw.append((f, cx, fi, ch, sw, scar_count.get((f.repo_name, f.path), 0) + scar_count.get(("", f.path), 0)))
    max_cx = max(r[1] for r in raw)
    max_fi = max(r[2] for r in raw)
    max_ch = max(r[3] for r in raw)
    max_sw = max(r[4] for r in raw)
    out: list[Hotspot] = []
    for f, cx, fi, ch, sw, sc in raw:
        factors = {"complexity": _norm(cx, max_cx), "fan_in": _norm(fi, max_fi), "churn": _norm(ch, max_ch),
                   "scars": _norm(sw, max_sw)}
        score = round(100.0 * sum(WEIGHTS[k] * v for k, v in factors.items()), 1)
        total, worst, worst_name, nfun = complexity.get(f.id, (0, 0, "", 0))
        reasons: list[str] = []
        if worst >= 10:
            reasons.append(f"complex function {worst_name.rsplit('.', 1)[-1]} (cyclomatic ~{worst})")
        if fi >= 3:
            reasons.append(f"{fi} files depend on it")
        if ch >= 3:
            reasons.append(f"changed in {ch} recent commits")
        if sc:
            reasons.append(f"{sc} scar(s) recorded")
        out.append(Hotspot(
            repo=f.repo_name, file=f.path, language=f.language, risk_score=score, level=_level(score),
            complexity=total, max_function_complexity=worst, worst_function=worst_name, functions=nfun,
            fan_in=fi, churn=ch, scars=sc, lines=f.line_count,
            factors={k: round(v, 3) for k, v in factors.items()}, reasons=reasons,
        ))
    out.sort(key=lambda h: (-h.risk_score, h.file))
    return out
