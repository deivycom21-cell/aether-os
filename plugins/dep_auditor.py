"""Dependency auditor: flags unpinned / floating / URL-sourced dependencies and risky install scripts in
``pyproject.toml``, ``requirements*.txt`` and ``package.json`` — fully offline, no registry calls.

Findings are written to ``security_findings`` (kind ``dependency``; previous audit rows are replaced on
every run so counts never inflate).
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from plugins.base import BaseAetherPlugin

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    tomllib = None  # type: ignore[assignment]

LIFECYCLE_SCRIPTS = frozenset({"preinstall", "install", "postinstall", "prepare", "prepublish", "prepublishOnly", "prepack", "postpack"})
RISKY_COMMAND = re.compile(
    r"\b(curl|wget|iwr|Invoke-WebRequest|Invoke-Expression|iex|powershell|pwsh|bash\s+-c|sh\s+-c|eval|node\s+-e|"
    r"python\s+-c|base64|chmod\s+777|rm\s+-rf\s+/|nc\s+-e|certutil)\b|https?://",
    re.I,
)
PIPE_TO_SHELL = re.compile(r"(curl|wget|iwr|Invoke-WebRequest)[^|]*\|\s*(sh|bash|zsh|iex|powershell|pwsh|node|python)", re.I)
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)(\[[^\]]*\])?\s*(.*)$")
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def classify_python_spec(raw: str) -> tuple[str, str, str] | None:
    """Return (name, issue, severity) for a PEP 508 requirement string, or None when it is pinned/bounded."""
    req = raw.split(";", 1)[0].strip()
    if not req or req.startswith("#"):
        return None
    if " @ " in req or req.startswith(("git+", "http://", "https://")):
        name = req.split("@", 1)[0].strip() or req
        return name, "installed from a mutable URL/VCS reference (not reproducible, bypasses the index)", "high"
    m = _REQ_NAME.match(req)
    if not m:
        return None
    name, spec = m.group(1), m.group(3).strip().strip("()")
    if not spec:
        return name, "unpinned (any version may be installed)", "medium"
    if spec in {"*", "==*"}:
        return name, "wildcard version", "medium"
    ops = re.findall(r"(===|==|~=|!=|<=|>=|<|>)", spec)
    if "==" in ops or "===" in ops or "~=" in ops:
        return None
    if any(o in {"<", "<="} for o in ops):
        return None
    if ops:
        return name, f"floating lower bound only ({spec}); major upgrades can break the build", "low"
    return None


def classify_npm_version(version: str) -> tuple[str, str] | None:
    v = str(version).strip()
    if v in {"", "*", "latest", "x", "next"} or v.lower() in {"x.x", "x.x.x"}:
        return "unpinned / tag-based version (any release may be installed)", "medium"
    if re.match(r"^(git\+|git:|github:|gitlab:|bitbucket:|https?://)", v) or re.match(r"^[\w.-]+/[\w.-]+(#.*)?$", v):
        return "installed from a git/URL reference (mutable, bypasses the registry)", "high"
    if v.startswith(">") and "<" not in v:
        return f"floating lower bound only ({v})", "low"
    return None


def audit_pyproject(text: str) -> list[dict[str, Any]]:
    deps: list[tuple[str, str]] = []
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            data = {}
        project = data.get("project", {}) if isinstance(data, dict) else {}
        deps += [("dependencies", d) for d in project.get("dependencies", []) if isinstance(d, str)]
        for group, items in (project.get("optional-dependencies") or {}).items():
            deps += [(f"optional:{group}", d) for d in items if isinstance(d, str)]
        poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data, dict) else {}
        for section in ("dependencies", "dev-dependencies"):
            for name, spec in (poetry.get(section) or {}).items():
                if name.lower() == "python":
                    continue
                if isinstance(spec, dict):
                    if "git" in spec or "url" in spec:
                        deps.append((f"poetry:{section}", f"{name} @ {spec.get('git') or spec.get('url')}"))
                        continue
                    spec = spec.get("version", "")
                spec = str(spec)
                deps.append((f"poetry:{section}", name if spec in {"*", ""} else f"{name}{'' if spec[:1] in '<>=!~^' else '=='}{spec}".replace("^", "~=")))
    else:
        block = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)\]", text)
        if block:
            deps += [("dependencies", d) for d in re.findall(r"[\"']([^\"']+)[\"']", block.group(1))]
    findings = []
    for section, d in deps:
        res = classify_python_spec(d)
        if res:
            findings.append({"package": res[0], "spec": d, "section": section, "issue": res[1], "severity": res[2]})
    return findings


def audit_requirements(text: str) -> list[dict[str, Any]]:
    findings = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("--extra-index-url", "--index-url", "-i ", "--find-links", "-f ")):
            findings.append({"package": "", "spec": line, "section": f"line {lineno}", "severity": "high",
                             "issue": "custom package index: dependency-confusion / typosquatting exposure"})
            continue
        if line.startswith("--trusted-host"):
            findings.append({"package": "", "spec": line, "section": f"line {lineno}", "severity": "medium",
                             "issue": "TLS verification disabled for a host"})
            continue
        if line.startswith(("-r", "-c", "--requirement", "--constraint", "-e .", "-e ./")):
            continue
        if line.startswith(("-e", "--editable")):
            line = line.split(None, 1)[1] if " " in line else ""
        res = classify_python_spec(line.split(" --hash", 1)[0])
        if res:
            findings.append({"package": res[0], "spec": line, "section": f"line {lineno}", "issue": res[1], "severity": res[2]})
    return findings


def audit_package_json(text: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [{"package": "", "spec": "", "section": "file", "issue": "invalid JSON", "severity": "low"}], {}
    if not isinstance(data, dict):
        return [], {}
    findings: list[dict[str, Any]] = []
    stats = {"dependencies": 0, "caret_or_tilde": 0}
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name, version in (data.get(section) or {}).items():
            stats["dependencies"] += 1
            if str(version).startswith(("^", "~")):
                stats["caret_or_tilde"] += 1
            res = classify_npm_version(str(version))
            if res and section != "peerDependencies":
                findings.append({"package": name, "spec": str(version), "section": section, "issue": res[0], "severity": res[1]})
    for script, cmd in (data.get("scripts") or {}).items():
        cmd = str(cmd)
        if PIPE_TO_SHELL.search(cmd):
            findings.append({"package": "", "spec": cmd[:200], "section": f"scripts.{script}", "severity": "critical",
                             "issue": "downloads remote code and pipes it into an interpreter"})
        elif script in LIFECYCLE_SCRIPTS and RISKY_COMMAND.search(cmd):
            findings.append({"package": "", "spec": cmd[:200], "section": f"scripts.{script}", "severity": "high",
                             "issue": f"'{script}' runs automatically on install and executes network/shell commands"})
    return findings, stats


class DependencyAuditor(BaseAetherPlugin):
    name = "dep_auditor"
    description = "Audits pyproject.toml, requirements*.txt and package.json for unpinned versions and risky scripts (offline)."

    def _manifests(self) -> list[tuple[str, Path, str]]:
        roots = dict(self.repo_roots())
        out = []
        for f in self.kernel.db.list_files():
            name = PurePosixPath(f.path).name
            if f.repo_name in roots and (name in {"pyproject.toml", "package.json"} or re.match(r"requirements.*\.txt$", name)):
                out.append((f.repo_name, roots[f.repo_name], f.path))
        for repo, root in roots.items():  # requirements files are sometimes git-ignored generated output
            for p in sorted(root.glob("requirements*.txt")):
                rel = p.relative_to(root).as_posix()
                if (repo, root, rel) not in out:
                    out.append((repo, root, rel))
        return out

    def is_applicable(self) -> bool:
        return bool(self._manifests())

    def run(self) -> dict[str, Any]:
        db = self.kernel.db
        db.execute("DELETE FROM security_findings WHERE kind = 'dependency'")
        reports: list[dict[str, Any]] = []
        totals: dict[str, int] = {}
        for repo, root, rel in self._manifests():
            try:
                text = (root / Path(*PurePosixPath(rel).parts)).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name = PurePosixPath(rel).name
            stats: dict[str, int] = {}
            if name == "pyproject.toml":
                findings = audit_pyproject(text)
            elif name == "package.json":
                findings, stats = audit_package_json(text)
            else:
                findings = audit_requirements(text)
            findings.sort(key=lambda f: SEVERITY_RANK.get(f["severity"], 9))
            for f in findings:
                totals[f["severity"]] = totals.get(f["severity"], 0) + 1
                db.add_security_finding("dependency", f"{f['severity']}:{f['package'] or f['section']}", f["severity"], rel, 0,
                                        f"{f['issue']} [{f['spec'][:120]}]")
            reports.append({"repo": repo, "file": rel, "findings": findings, "stats": stats})
        worst = min((SEVERITY_RANK[s] for s in totals), default=9)
        status = next((k for k, v in SEVERITY_RANK.items() if v == worst), "ok") if totals else "ok"
        return {"manifests": len(reports), "findings": sum(totals.values()), "by_severity": totals, "status": status,
                "reports": reports}
