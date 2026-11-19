"""Swarm orchestrator: parallel roles in isolated git worktrees, automatic verification, safe merge.

Lifecycle (``aether swarm ...``)::

    launch  -> one branch + worktree per role under .aether/worktrees/<run>/<role> and a brief per role
    execute -> (optional, BYOK) architect writes PLAN.md, builder writes code, tester writes tests
    verify  -> integration worktree merges role branches, runs the test command + security scan
    merge   -> snapshot, then --no-ff merge of the verified integration branch into the current branch
    cleanup -> remove worktrees and role branches

Agents (Claude Code, Cursor, Aider...) can also work manually inside each worktree using the brief.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from aether_core.security import PromptInjectionDetector, detect_secrets
from aether_swarm.llm import LLMClient, LLMUnavailableError
from aether_swarm.router import ComplexityRouter

if TYPE_CHECKING:
    from aether_core.kernel import AetherKernel

ROLES = ("architect", "builder", "tester", "security_auditor")
ROLE_MISSIONS = {
    "architect": "Design the change: modules to touch, interfaces, data flow, risks and an ordered step list. Do not write production code.",
    "builder": "Implement the feature in production code following the plan. Keep changes minimal and idiomatic.",
    "tester": "Write automated tests that prove the feature works and guard against regressions, including edge cases.",
    "security_auditor": "Review the change for leaked secrets, injection vectors, unsafe input handling and missing authz.",
}
ROLE_PROMPT_PREFIX = {
    "architect": "Design the architecture for",
    "builder": "Implement",
    "tester": "Write tests for",
    "security_auditor": "Security review of",
}
FILE_BLOCK = re.compile(r"<<<FILE\s+([^\n>]+?)\s*>>>\n(.*?)\n<<<END>>>", re.S)
GIT_TIMEOUT = 120


class SwarmError(RuntimeError):
    """Raised for invalid swarm state or failed git operations; messages are user-facing."""


def _git(cwd: Path, *args: str, check: bool = True, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if not git:
        raise SwarmError("git executable not found on PATH")
    proc = subprocess.run([git, "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    if check and proc.returncode != 0:
        raise SwarmError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def _commit_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "AetherOS Swarm")
    env.setdefault("GIT_AUTHOR_EMAIL", "swarm@aether.local")
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
    return env


def _git_commit_all(cwd: Path, message: str) -> bool:
    _git(cwd, "add", "-A")
    if not _git(cwd, "status", "--porcelain", check=False).stdout.strip():
        return False
    git = shutil.which("git") or "git"
    proc = subprocess.run([git, "-C", str(cwd), "commit", "-m", message, "--no-verify"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=_commit_env(), timeout=GIT_TIMEOUT)
    if proc.returncode != 0:
        raise SwarmError(f"git commit failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return True


def detect_test_command(path: Path) -> list[str] | None:
    override = os.environ.get("AETHER_SWARM_TEST_CMD")
    if override:
        parts = shlex.split(override, posix=os.name != "nt")
        return [x[1:-1] if len(x) >= 2 and x[0] == x[-1] and x[0] in "\"'" else x for x in parts]
    if (path / "pyproject.toml").is_file() or (path / "pytest.ini").is_file() or (path / "tests").is_dir():
        return [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]
    pkg = path / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, json.JSONDecodeError):
            scripts = {}
        npm = shutil.which("npm")
        if "test" in scripts and npm:
            return [npm, "test", "--silent"]
    if (path / "go.mod").is_file() and shutil.which("go"):
        return [shutil.which("go") or "go", "test", "./..."]
    if (path / "Cargo.toml").is_file() and shutil.which("cargo"):
        return [shutil.which("cargo") or "cargo", "test", "--quiet"]
    return None


def parse_file_blocks(text: str) -> list[tuple[str, str]]:
    return [(m.group(1).strip(), m.group(2)) for m in FILE_BLOCK.finditer(text)]


def safe_write(worktree: Path, rel: str, content: str) -> Path:
    rel_norm = PurePosixPath(rel.replace("\\", "/"))
    if rel_norm.is_absolute() or ".." in rel_norm.parts or not rel_norm.parts or rel_norm.parts[0] in {".git", ".aether"}:
        raise SwarmError(f"Refusing to write outside the worktree: {rel}")
    target = (worktree / Path(*rel_norm.parts)).resolve()
    if worktree.resolve() not in target.parents:
        raise SwarmError(f"Refusing to write outside the worktree: {rel}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
    return target


@dataclass(slots=True)
class RoleState:
    role: str
    branch: str
    worktree: str
    brief: str
    routing: dict[str, Any]
    status: str = "ready"
    commits: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "branch": self.branch, "worktree": self.worktree, "brief": self.brief,
                "routing": self.routing, "status": self.status, "commits": self.commits, "notes": self.notes}


class SwarmOrchestrator:
    def __init__(self, kernel: AetherKernel, router: ComplexityRouter | None = None, llm: LLMClient | None = None) -> None:
        self.k = kernel
        self.router = router or ComplexityRouter()
        self.llm = llm or LLMClient(self.router.ollama)
        self.base_dir = kernel.aether_dir / "worktrees"

    # ------------------------------------------------------------------ helpers
    def _step(self, run_id: str, role: str, step: str, **payload: Any) -> None:
        self.k.bus.publish("SWARM_STEP", {"run_id": run_id, "role": role, "step": step, **payload},
                           [f"swarm:{run_id}:{role}"])

    def _save(self, run: dict[str, Any]) -> None:
        self.k.db.save_swarm_run(run["id"], run["description"], run["status"], run)

    def load(self, run_id: str) -> dict[str, Any]:
        row = self.k.db.get_swarm_run(run_id)
        if row is None:
            raise SwarmError(f"Unknown swarm run: {run_id}")
        return row["data"]

    def ensure_git_ready(self) -> str:
        root = self.k.root
        if _git(root, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
            raise SwarmError("The project is not a git repository. Run `git init` and commit first.")
        head = _git(root, "rev-parse", "--verify", "HEAD", check=False)
        if head.returncode != 0:
            raise SwarmError("The repository has no commits yet; create an initial commit before launching a swarm.")
        return head.stdout.strip()

    # ------------------------------------------------------------------ plan / launch
    def plan(self, description: str, roles: tuple[str, ...] = ROLES) -> list[dict[str, Any]]:
        context = self.k.query_smart_context(description, max_tokens=1200)
        relevant = sorted({r["file"] for r in context.get("results", [])})[:12]
        plan = []
        for role in roles:
            if role not in ROLE_MISSIONS:
                raise SwarmError(f"Unknown role '{role}'. Valid roles: {', '.join(ROLES)}")
            subtask = f"{ROLE_PROMPT_PREFIX[role]}: {description}"
            decision = self.router.route(f"{subtask}\n{ROLE_MISSIONS[role]}", context_tokens=context.get("metrics", {}).get("compressed_tokens", 1200))
            plan.append({"role": role, "subtask": subtask, "mission": ROLE_MISSIONS[role], "routing": decision.to_dict(),
                         "relevant_files": relevant})
        return plan

    def _brief(self, run_id: str, description: str, item: dict[str, Any], base: str, context: dict[str, Any]) -> str:
        scars = context.get("scars", [])
        lines = [
            f"# AetherOS swarm brief — {item['role']}",
            "",
            f"- Run: `{run_id}`  |  Base commit: `{base[:12]}`",
            f"- Suggested model tier: **{item['routing']['tier']}** (`{item['routing']['model']}`), complexity {item['routing']['complexity']}",
            "",
            "## Feature",
            description,
            "",
            "## Your mission",
            item["mission"],
            "",
            "## Relevant files (from the Aether graph)",
            *([f"- `{f}`" for f in item["relevant_files"]] or ["- (no strong matches; explore with query_smart_context)"]),
            "",
            "## Scars on these files",
            *([f"- [{s['severity']}] `{s['file']}`: {s['error']} → fix: {s['fix']}" for s in scars] or ["- none recorded"]),
            "",
            "## Rules",
            "- Work only inside this worktree and commit on its branch.",
            "- Call `query_smart_context` / `check_scars` (Aether MCP) before opening large files.",
            "- Never commit secrets. `aether swarm-verify` runs tests and a secret scan before any merge.",
        ]
        return "\n".join(lines) + "\n"

    def launch(self, description: str, roles: tuple[str, ...] = ROLES, execute: bool = False) -> dict[str, Any]:
        if not description.strip():
            raise SwarmError("The feature description must not be empty.")
        base = self.ensure_git_ready()
        base_branch = _git(self.k.root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        run_id = time.strftime("sw-%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        run_dir = self.base_dir / run_id
        (run_dir / "briefs").mkdir(parents=True, exist_ok=True)
        plan = self.plan(description, roles)
        context = self.k.query_smart_context(description, max_tokens=1200)
        run: dict[str, Any] = {
            "id": run_id, "description": description, "status": "launched", "base_commit": base,
            "base_branch": base_branch, "created_at": time.time(), "roles": {}, "verification": None,
            "cost_estimate": {
                "total_usd": round(sum(p["routing"]["est_cost_usd"] for p in plan), 6),
                "baseline_usd": round(sum(p["routing"]["baseline_cost_usd"] for p in plan), 6),
            },
        }
        self._step(run_id, "orchestrator", "launch", description=description[:160])
        for item in plan:
            role = item["role"]
            branch = f"aether/{run_id}/{role}"
            wt = run_dir / role
            _git(self.k.root, "worktree", "add", "-b", branch, str(wt), base)
            brief_path = run_dir / "briefs" / f"{role}.md"
            brief_path.write_text(self._brief(run_id, description, item, base, context), encoding="utf-8")
            run["roles"][role] = RoleState(role, branch, str(wt), str(brief_path), item["routing"]).to_dict()
            self._step(run_id, role, "worktree_ready", tier=item["routing"]["tier"], model=item["routing"]["model"])
        self._save(run)
        if execute:
            run = self.execute(run_id)
        return run

    # ------------------------------------------------------------------ execute (BYOK)
    def execute(self, run_id: str) -> dict[str, Any]:
        run = self.load(run_id)
        avail = self.llm.available()
        if not any(avail.values()):
            run["status"] = "awaiting_agents"
            run.setdefault("notes", []).append(
                "No LLM credentials found (ANTHROPIC_API_KEY / local Ollama). Worktrees and briefs are ready for "
                "manual agents; run `aether swarm-verify` when they have committed."
            )
            self._save(run)
            return run
        plan_text = ""
        roles = run["roles"]
        try:
            if "architect" in roles:
                plan_text = self._run_architect(run, roles["architect"])
            if "builder" in roles:
                self._run_codegen(run, roles["builder"], plan_text, "production code implementing the feature")
            if "tester" in roles:
                tester_wt = Path(roles["tester"]["worktree"])
                if "builder" in roles and roles["builder"]["commits"]:
                    _git(tester_wt, "merge", "--ff-only", roles["builder"]["branch"])
                self._run_codegen(run, roles["tester"], plan_text, "automated tests for the feature (tests only)")
            run["status"] = "executed"
        except (LLMUnavailableError, SwarmError) as exc:
            run["status"] = "execution_failed"
            run.setdefault("notes", []).append(str(exc))
            self._step(run_id, "orchestrator", "execution_failed", error=str(exc)[:200])
        self._save(run)
        return run

    def _llm_for(self, role_state: dict[str, Any]) -> Any:
        from aether_swarm.router import RoutingDecision, Tier, tier_models

        r = role_state["routing"]
        tier = Tier(r["tier"])
        avail = self.llm.available()
        if tier == Tier.LOCAL_OLLAMA and not avail["ollama"]:
            tier = Tier.FAST_MODEL
        if tier != Tier.LOCAL_OLLAMA and not avail["anthropic"] and avail["ollama"]:
            tier = Tier.LOCAL_OLLAMA
        if tier == Tier(r["tier"]):
            model = r["model"]
        elif tier == Tier.LOCAL_OLLAMA:
            model = (self.router.ollama.models() or ["llama3.1"])[0]
        else:
            model = tier_models()[Tier.FAST_MODEL]
        return RoutingDecision(tier=tier, model=model, complexity=float(r["complexity"]))

    def _run_architect(self, run: dict[str, Any], st: dict[str, Any]) -> str:
        brief = Path(st["brief"]).read_text(encoding="utf-8")
        self._step(run["id"], "architect", "thinking", model=st["routing"]["model"])
        resp = self.llm.complete(self._llm_for(st), "You are a principal software architect. Answer in Markdown.",
                                 f"{brief}\n\nProduce PLAN.md: goals, files to change, interfaces, risks, ordered steps.")
        plan_text = self.k.vault.mask(resp.text).text
        wt = Path(st["worktree"])
        (wt / "docs").mkdir(exist_ok=True)
        (wt / "docs" / f"PLAN-{run['id']}.md").write_text(plan_text, encoding="utf-8")
        if _git_commit_all(wt, f"aether swarm {run['id']}: architecture plan"):
            st["commits"] += 1
        st["status"] = "done"
        st["notes"].append(resp.to_dict())
        self._step(run["id"], "architect", "done", tokens=resp.output_tokens)
        return plan_text

    def _run_codegen(self, run: dict[str, Any], st: dict[str, Any], plan_text: str, what: str) -> None:
        role = st["role"]
        wt = Path(st["worktree"])
        brief = Path(st["brief"]).read_text(encoding="utf-8")
        files_ctx = []
        for line in brief.splitlines():
            m = re.match(r"- `([^`]+)`$", line.strip())
            if m and (wt / m.group(1)).is_file():
                content = (wt / m.group(1)).read_text(encoding="utf-8", errors="replace")
                files_ctx.append(f"### {m.group(1)}\n```\n{content[:6000]}\n```")
        prompt = (
            f"{brief}\n\n## Plan\n{plan_text or '(no plan)'}\n\n## Current files\n" + "\n\n".join(files_ctx[:6])
            + f"\n\nWrite {what}. Output ONLY complete files using this exact format, one block per file:\n"
            "<<<FILE relative/path.ext>>>\n<full file content>\n<<<END>>>\n"
        )
        self._step(run["id"], role, "generating", model=st["routing"]["model"])
        resp = self.llm.complete(self._llm_for(st), "You are a senior engineer. Follow the output format exactly.", prompt)
        blocks = parse_file_blocks(resp.text)
        if not blocks:
            raise SwarmError(f"{role}: the model returned no file blocks")
        written = []
        for rel, content in blocks:
            if detect_secrets(content):
                raise SwarmError(f"{role}: generated content for {rel} contains secret-like material; refusing to write")
            safe_write(wt, rel, content)
            written.append(rel)
        if _git_commit_all(wt, f"aether swarm {run['id']}: {role}"):
            st["commits"] += 1
        st["status"] = "done"
        st["notes"].append({**resp.to_dict(), "files": written})
        self._step(run["id"], role, "committed", files=len(written))

    # ------------------------------------------------------------------ status
    def status(self, run_id: str) -> dict[str, Any]:
        run = self.load(run_id)
        base = run["base_commit"]
        for role, st in run["roles"].items():
            br = st["branch"]
            exists = _git(self.k.root, "rev-parse", "--verify", br, check=False).returncode == 0
            if not exists:
                st["status"] = "missing"
                continue
            st["commits"] = int(_git(self.k.root, "rev-list", "--count", f"{base}..{br}").stdout.strip() or 0)
            st["changed_files"] = [f for f in _git(self.k.root, "diff", "--name-only", f"{base}...{br}").stdout.splitlines() if f]
            wt = Path(st["worktree"])
            st["dirty"] = bool(wt.is_dir() and _git(wt, "status", "--porcelain", check=False).stdout.strip())
        self._save(run)
        return run

    def list_runs(self) -> list[dict[str, Any]]:
        return self.k.db.list_swarm_runs(50)

    # ------------------------------------------------------------------ verify
    def verify(self, run_id: str, run_tests: bool = True) -> dict[str, Any]:
        run = self.status(run_id)
        base = run["base_commit"]
        integ_branch = f"aether/{run_id}/integration"
        integ_wt = self.base_dir / run_id / "integration"
        if integ_wt.exists():
            _git(self.k.root, "worktree", "remove", "--force", str(integ_wt), check=False)
        _git(self.k.root, "branch", "-D", integ_branch, check=False)
        _git(self.k.root, "worktree", "add", "-b", integ_branch, str(integ_wt), base)
        self._step(run_id, "orchestrator", "integrating")
        merged, conflicts = [], []
        for role in ("architect", "builder", "tester", "security_auditor"):
            st = run["roles"].get(role)
            if not st or not st.get("commits"):
                continue
            proc = subprocess.run(
                [shutil.which("git") or "git", "-C", str(integ_wt), "merge", "--no-ff", "--no-edit", st["branch"]],
                capture_output=True, text=True, encoding="utf-8", errors="replace", env=_commit_env(), timeout=GIT_TIMEOUT,
            )
            if proc.returncode != 0:
                conflicts.append({"role": role, "output": (proc.stdout + proc.stderr).strip()[-800:]})
                _git(integ_wt, "merge", "--abort", check=False)
            else:
                merged.append(role)
        changed = [f for f in _git(integ_wt, "diff", "--name-only", f"{base}...HEAD").stdout.splitlines() if f]
        # Security audit of every changed file.
        detector = PromptInjectionDetector()
        secrets_found: list[dict[str, Any]] = []
        injections: list[dict[str, Any]] = []
        for rel in changed:
            p = integ_wt / rel
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            secrets_found += [{"file": rel, **f.to_dict()} for f in detect_secrets(text)]
            rep = detector.analyze(text)
            if rep.is_suspicious:
                injections.append({"file": rel, **rep.to_dict()})
        self._step(run_id, "security_auditor", "audit", secrets=len(secrets_found), injections=len(injections))
        tests: dict[str, Any] = {"ran": False}
        cmd = detect_test_command(integ_wt) if run_tests else None
        if cmd:
            self._step(run_id, "tester", "running_tests", command=" ".join(cmd)[:120])
            t0 = time.perf_counter()
            env = dict(os.environ)
            env["PYTHONPATH"] = str(integ_wt) + os.pathsep + env.get("PYTHONPATH", "")
            try:
                proc = subprocess.run(cmd, cwd=str(integ_wt), capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=900, env=env)
                tests = {"ran": True, "command": cmd, "returncode": proc.returncode, "passed": proc.returncode == 0,
                         "duration_s": round(time.perf_counter() - t0, 2), "output_tail": (proc.stdout + proc.stderr)[-2500:]}
            except subprocess.TimeoutExpired:
                tests = {"ran": True, "command": cmd, "returncode": -1, "passed": False, "output_tail": "timed out after 900s"}
        ok = not conflicts and not secrets_found and not any(i["severity"] in {"high", "critical"} for i in injections) \
            and (tests.get("passed", True) if tests.get("ran") else True)
        run = self.load(run_id)
        run["verification"] = {
            "integration_branch": integ_branch, "integration_worktree": str(integ_wt), "merged_roles": merged,
            "conflicts": conflicts, "changed_files": changed, "secrets": secrets_found, "injections": injections,
            "tests": tests, "ok": ok, "verified_at": time.time(),
        }
        run["status"] = "verified" if ok else "verification_failed"
        self._save(run)
        self._step(run_id, "orchestrator", run["status"], changed=len(changed))
        return run

    # ------------------------------------------------------------------ merge / cleanup
    def merge(self, run_id: str, force: bool = False) -> dict[str, Any]:
        run = self.load(run_id)
        ver = run.get("verification") or {}
        if not force and not ver.get("ok"):
            raise SwarmError("Run is not verified. Execute `aether swarm-verify` first (or pass --force).")
        if not ver.get("integration_branch"):
            raise SwarmError("No integration branch; run verification first.")
        dirty = [
            ln for ln in _git(self.k.root, "status", "--porcelain").stdout.splitlines()
            if ln.strip() and not ln[3:].strip('"').startswith(".aether/")  # Aether's own state never blocks a merge
        ]
        if dirty:
            raise SwarmError("Working tree has uncommitted changes; commit or stash them before merging.")
        snap = self.k.create_snapshot(f"before swarm merge {run_id}")
        proc = subprocess.run(
            [shutil.which("git") or "git", "-C", str(self.k.root), "merge", "--no-ff", "-m",
             f"aether swarm {run_id}: {run['description'][:60]}", ver["integration_branch"]],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=_commit_env(), timeout=GIT_TIMEOUT,
        )
        if proc.returncode != 0:
            _git(self.k.root, "merge", "--abort", check=False)
            raise SwarmError(f"Merge failed and was aborted: {(proc.stdout + proc.stderr).strip()[-600:]}")
        run["status"] = "merged"
        run["merged_at"] = time.time()
        run["pre_merge_snapshot"] = snap["id"]
        self._save(run)
        self._step(run_id, "orchestrator", "merged", snapshot=snap["id"])
        self.k.index(run_plugins=False)
        return run

    def cleanup(self, run_id: str, delete_branches: bool = True) -> dict[str, Any]:
        run = self.load(run_id)
        removed = []
        worktrees = [Path(st["worktree"]) for st in run["roles"].values()]
        if run.get("verification"):
            worktrees.append(Path(run["verification"]["integration_worktree"]))
        for wt in worktrees:
            if wt.exists():
                _git(self.k.root, "worktree", "remove", "--force", str(wt), check=False)
                if wt.exists():
                    shutil.rmtree(wt, ignore_errors=True)
                removed.append(str(wt))
        _git(self.k.root, "worktree", "prune", check=False)
        if delete_branches:
            branches = [st["branch"] for st in run["roles"].values()] + [f"aether/{run_id}/integration"]
            for br in branches:
                _git(self.k.root, "branch", "-D", br, check=False)
        if run["status"] != "merged":
            run["status"] = "cleaned"
        self._save(run)
        self._step(run_id, "orchestrator", "cleaned", worktrees=len(removed))
        return {"run_id": run_id, "removed_worktrees": removed, "status": run["status"]}
