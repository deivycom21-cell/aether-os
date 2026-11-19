"""AetherKernel: the single facade shared by the CLI, MCP server, web dashboard and TUI."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aether_core import AETHER_DIRNAME, __version__
from aether_core.ast_indexer import GraphResolver, IndexStats, Indexer
from aether_core.budget import DEFAULT_BUDGET_TOKENS, BudgetTracker
from aether_core.context_engine import ContextEngine
from aether_core.database import AetherDB, normalize_rel_path, normalize_severity
from aether_core.event_bus import EventBus
from aether_core.git_sync import TEAM_BRAIN_FILENAME, SnapshotManager, export_team_brain, import_team_brain
from aether_core.policy import POLICY_FILENAME, Policy
from aether_core.security import PromptInjectionDetector, SecretVault, SecurityScanner

CONFIG_FILENAME = "config.json"
DB_FILENAME = "brain.db"
AETHER_GITIGNORE = "# Managed by AetherOS: only the team brain is shared through git.\n*\n!.gitignore\n!team_brain.json.gz\n"


@dataclass(slots=True)
class AetherConfig:
    linked_repos: list[dict[str, str]] = field(default_factory=list)
    trusted_plugins: list[str] = field(default_factory=list)
    disabled_plugins: list[str] = field(default_factory=list)
    web_port: int = 7777
    budget_tokens: int = DEFAULT_BUDGET_TOKENS

    @classmethod
    def load(cls, path: Path) -> AetherConfig:
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls(
            linked_repos=list(data.get("linked_repos", [])),
            trusted_plugins=list(data.get("trusted_plugins", [])),
            disabled_plugins=list(data.get("disabled_plugins", [])),
            web_port=int(data.get("web_port", 7777)),
            budget_tokens=max(1000, int(data.get("budget_tokens", DEFAULT_BUDGET_TOKENS))),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "linked_repos": self.linked_repos,
                    "trusted_plugins": self.trusted_plugins,
                    "disabled_plugins": self.disabled_plugins,
                    "web_port": self.web_port,
                    "budget_tokens": self.budget_tokens,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` looking for ``.aether`` (then ``.git``); fall back to ``start``."""
    start = (start or Path.cwd()).resolve()
    for marker in (AETHER_DIRNAME, ".git"):
        for candidate in (start, *start.parents):
            if (candidate / marker).exists():
                return candidate
    return start


class AetherKernel:
    def __init__(self, root: Path | str | None = None, db_path: Path | None = None) -> None:
        env_root = os.environ.get("AETHER_ROOT")
        self.root = Path(root).resolve() if root else find_project_root(Path(env_root) if env_root else None)
        self.aether_dir = self.root / AETHER_DIRNAME
        self.aether_dir.mkdir(parents=True, exist_ok=True)
        gi = self.aether_dir / ".gitignore"
        if not gi.exists():
            gi.write_text(AETHER_GITIGNORE, encoding="utf-8")
        self.config_path = self.aether_dir / CONFIG_FILENAME
        self.config = AetherConfig.load(self.config_path)
        self.db = AetherDB(db_path or self.aether_dir / DB_FILENAME)
        self.bus = EventBus(self.db)
        self.vault = SecretVault()
        self.injection = PromptInjectionDetector()
        self.indexer = Indexer(self.db)
        self.resolver = GraphResolver(self.db)
        self.context = ContextEngine(self.db, self.bus, self.vault, self.injection)
        self.snapshots = SnapshotManager(self.root, self.aether_dir, self.db, self.bus)
        self.budget = BudgetTracker(self.db, self.bus, self.config.budget_tokens)
        self._policy = Policy.load(self.aether_dir)
        self._policy_mtime = self._policy_file_mtime()
        self.context.read_policy = lambda: self.policy.can_read
        self._index_lock = threading.Lock()
        self._hotspot_cache: tuple[tuple[Any, ...], list[Any]] | None = None
        self.started_at = time.time()

    # ------------------------------------------------------------------ policy
    def _policy_file_mtime(self) -> float:
        try:
            return (self.aether_dir / POLICY_FILENAME).stat().st_mtime
        except OSError:
            return 0.0

    @property
    def policy(self) -> Policy:
        """Current ``.aether/policy.json`` (re-read whenever the file changes on disk)."""
        mtime = self._policy_file_mtime()
        if mtime != self._policy_mtime:
            self._policy = Policy.load(self.aether_dir)
            self._policy_mtime = mtime
        return self._policy

    def save_policy(self, policy: Policy) -> Path:
        path = policy.save(self.aether_dir)
        self._policy = policy
        self._policy_mtime = self._policy_file_mtime()
        return path

    def check_path_policy(self, path: str) -> dict[str, Any]:
        return self.policy.explain(path)

    def repo_roots(self) -> dict[str, Path]:
        roots = {self.repo_name: self.root}
        for link in self.config.linked_repos:
            lp = Path(link["path"])
            if lp.is_dir():
                roots[link["name"]] = lp.resolve()
        return roots

    # ------------------------------------------------------------------ repos
    @property
    def repo_name(self) -> str:
        return self.root.name

    def link_repo(self, path: Path | str, name: str | None = None) -> dict[str, str]:
        p = Path(path).expanduser()
        p = (self.root / p).resolve() if not p.is_absolute() else p.resolve()
        if not p.is_dir():
            raise FileNotFoundError(f"Linked repository not found: {p}")
        if p == self.root:
            raise ValueError("Cannot link the primary repository to itself.")
        entry = {"name": name or p.name, "path": str(p)}
        self.config.linked_repos = [r for r in self.config.linked_repos if r["path"] != entry["path"] and r["name"] != entry["name"]]
        self.config.linked_repos.append(entry)
        self.config.save(self.config_path)
        return entry

    def unlink_repo(self, name: str) -> bool:
        before = len(self.config.linked_repos)
        self.config.linked_repos = [r for r in self.config.linked_repos if r["name"] != name]
        self.config.save(self.config_path)
        if len(self.config.linked_repos) != before:
            rows = self.db.query("SELECT id FROM repos WHERE name = ?", (name,))
            for r in rows:
                self.db.execute("DELETE FROM repos WHERE id = ?", (r["id"],))
            self.db.bump_index_version()
            return True
        return False

    # ------------------------------------------------------------------ indexing
    def index(self, force: bool = False, run_plugins: bool = True) -> dict[str, Any]:
        with self._index_lock:
            t0 = time.perf_counter()
            results: list[IndexStats] = [self.indexer.index_repo(self.root, self.repo_name, primary=True, force=force)]
            for link in self.config.linked_repos:
                lp = Path(link["path"])
                if lp.is_dir():
                    results.append(self.indexer.index_repo(lp, link["name"], primary=False, force=force))
            resolved = self.resolver.resolve()
            plugin_reports: list[dict[str, Any]] = []
            if run_plugins:
                from plugins.base import PluginManager

                manager = PluginManager(self)
                plugin_reports = [r.to_dict() for r in manager.run_all()]
                resolved += self.resolver.resolve()
            version = self.db.bump_index_version()
            summary = {
                "repos": [r.to_dict() for r in results],
                "resolved_edges": resolved,
                "plugins": plugin_reports,
                "index_version": version,
                "counts": self.db.counts(),
                "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
            self.bus.publish(
                "INDEX_UPDATED",
                {"files": summary["counts"]["files"], "nodes": summary["counts"]["nodes"],
                 "changed": sum(r.indexed + r.removed for r in results), "duration_ms": summary["duration_ms"]},
            )
            return summary

    def reindex_paths(self, paths: list[Path]) -> int:
        """Incremental re-index for the file watcher."""
        changed = 0
        with self._index_lock:
            for p in paths:
                rp = p.resolve()
                repo_root, repo_name = self.root, self.repo_name
                for link in self.config.linked_repos:
                    lp = Path(link["path"]).resolve()
                    if lp in rp.parents:
                        repo_root, repo_name = lp, link["name"]
                        break
                if self.indexer.index_single_file(repo_root, rp, repo_name):
                    changed += 1
            if changed:
                self.resolver.resolve()
                self.db.bump_index_version()
                self.bus.publish("INDEX_UPDATED", {"changed": changed, "incremental": True})
        return changed

    # ------------------------------------------------------------------ agent tools
    def query_smart_context(self, query: str, max_tokens: int = 1500) -> dict[str, Any]:
        if not query.strip():
            return {"error": "query must not be empty"}
        return self.context.query(query, max_tokens=max_tokens)

    def impact_analysis(self, file_path: str) -> dict[str, Any]:
        if not self.policy.can_read(file_path):
            self.bus.publish("POLICY_BLOCKED", {"action": "impact_analysis", "file": normalize_rel_path(file_path)})
            return {"file": file_path, "found": False, "error": "denied by .aether/policy.json"}
        return self.context.impact_analysis(file_path)

    def record_scar(self, file_path: str, error_summary: str, fix_applied: str, severity: str = "high",
                    root_cause: str = "", symbol: str = "") -> dict[str, Any]:
        if not file_path.strip() or not error_summary.strip():
            return {"ok": False, "error": "file_path and error_summary are required"}
        masked_summary = self.vault.mask(error_summary).text
        masked_fix = self.vault.mask(fix_applied).text
        masked_cause = self.vault.mask(root_cause).text
        f = self.db.file_by_path(file_path)
        rel = f.path if f else file_path.replace("\\", "/")
        scar_id, created = self.db.add_scar(
            rel, masked_summary, masked_fix, normalize_severity(severity), masked_cause, symbol,
            source="manual", repo_name=f.repo_name if f else self.repo_name,
        )
        self.bus.publish("SCAR_RECORDED", {"id": scar_id, "file": rel, "severity": normalize_severity(severity), "created": created},
                         [f"{f.repo_name if f else self.repo_name}:{rel}"])
        return {"ok": True, "id": scar_id, "created": created, "file_path": rel, "severity": normalize_severity(severity)}

    def check_scars(self, file_path: str) -> list[dict[str, Any]]:
        scars = self.db.scars_for_path(file_path)
        if scars:
            f = self.db.file_by_path(file_path)
            key = f"{f.repo_name}:{f.path}" if f else f"{self.repo_name}:{file_path}"
            self.bus.publish("SCAR_TRIGGERED", {"count": len(scars), "files": [file_path]}, [key])
        return [s.to_dict() for s in scars]

    def create_snapshot(self, label: str = "manual") -> dict[str, Any]:
        return self.snapshots.create(label or "manual").to_dict()

    def rollback_snapshot(self, snapshot_id: str, prune_new: bool = True) -> dict[str, Any]:
        diff = self.snapshots.diff(snapshot_id)
        touched = [*diff["modified"], *diff["removed"], *(diff["added"] if prune_new else [])]
        blocked = [p for p in touched if not self.policy.can_write(p)]
        if blocked:
            self.bus.publish("POLICY_BLOCKED", {"action": "rollback", "snapshot_id": snapshot_id, "files": blocked[:20]})
            return {"ok": False, "error": "rollback would modify read-only/denied paths (.aether/policy.json)",
                    "blocked": blocked[:50], "restored": 0, "deleted_new_files": 0, "safety_snapshot": None}
        result = self.snapshots.rollback(snapshot_id, prune_new=prune_new)
        if result["restored"] or result["deleted_new_files"]:
            self.index(run_plugins=False)
        return result

    def scan_security(self, target_path: str = ".") -> dict[str, Any]:
        target = Path(target_path)
        target = (self.root / target).resolve() if not target.is_absolute() else target.resolve()
        if not target.exists():
            return {"error": f"Path not found: {target_path}"}
        if self.root != target and self.root not in target.parents and not any(
            Path(r["path"]).resolve() in (target, *target.parents) for r in self.config.linked_repos
        ):
            return {"error": "Target must be inside the project or a linked repository."}

        def on_secret(rel: str, finding: Any) -> None:
            self.db.add_security_finding("secret", finding.rule, finding.severity, rel, finding.line, finding.preview)

        def on_injection(rel: str, report: Any) -> None:
            self.db.add_security_finding("injection", ",".join(sorted({f.rule for f in report.findings})),
                                         report.severity, rel, report.findings[0].line if report.findings else 0, "")

        scanner = SecurityScanner(self.injection, on_secret, on_injection)
        report = scanner.scan_path(target, base=self.root if (self.root == target or self.root in target.parents) else target)
        data = report.to_dict()
        files_flagged = sorted({s["file"] for s in data["secrets"]} | {i["file"] for i in data["injections"]})
        keys = [f"{self.repo_name}:{p}" for p in files_flagged][:60]
        self.bus.publish("SECURITY_SCAN", {"risk_level": data["risk_level"], "secrets": data["secret_count"],
                                           "injections": data["injection_count"], "files": data["files_scanned"]}, keys)
        if data["secret_count"]:
            self.bus.publish("SECRET_BLOCKED", {"count": data["secret_count"], "context": "scan_security"},
                             [f"{self.repo_name}:{s['file']}" for s in data["secrets"]][:60])
        if data["injection_count"]:
            self.bus.publish("INJECTION_DETECTED", {"count": data["injection_count"], "context": "scan_security"},
                             [f"{self.repo_name}:{i['file']}" for i in data["injections"]][:60])
        return data

    def get_telemetry_stats(self) -> dict[str, Any]:
        totals = self.db.telemetry_totals()
        counts = self.db.counts()
        sec = self.db.security_counts()
        swarm = self.db.list_swarm_runs(5)
        return {
            "version": __version__,
            "project": self.repo_name,
            "root": str(self.root),
            "tokens_saved": totals["saved_tokens"],
            "usd_saved": totals["saved_usd"],
            "queries": totals["queries"],
            "raw_tokens": totals["raw_tokens"],
            "compressed_tokens": totals["compressed_tokens"],
            "avg_latency_ms": totals["avg_latency_ms"],
            "compression_ratio": round(totals["raw_tokens"] / totals["compressed_tokens"], 2) if totals["compressed_tokens"] else 0.0,
            "files_indexed": counts["files"],
            "graph": counts,
            "linked_repos": [r["name"] for r in self.config.linked_repos],
            "secrets_blocked": sec.get("secret_masked", 0) + sec.get("secret", 0),
            "injections_detected": sec.get("injection", 0),
            "swarm": {
                "runs": len(swarm),
                "active": [r for r in swarm if r["status"] not in {"merged", "failed", "aborted", "cleaned"}],
                "recent": swarm,
            },
            "series": self.db.telemetry_series(40),
            "latency": self.db.telemetry_latency_percentiles(),
            "budget": self.budget.status(),
            "dependency_findings": sec.get("dependency", 0),
            "uptime_s": round(time.time() - self.started_at, 1),
        }

    # ------------------------------------------------------------------ v0.2 · auto-fix loop
    def prepare_heal_context(self, command: str = "", output: str | None = None, max_tokens: int = 1500) -> dict[str, Any]:
        """Run ``command`` (policy-checked) or analyse ``output``; write ``.aether/last_heal_context.json``."""
        from aether_core.healer import Healer, HealError

        if not command.strip() and output is None:
            return {"ok": False, "error": "pass a command to run or the failing output to analyse"}
        try:
            return Healer(self).prepare(command, output=output, max_tokens=max_tokens)
        except HealError as exc:
            return {"ok": False, "error": str(exc)}

    def heal(self, command: str, execute: bool = False, max_attempts: int = 3, llm: Any = None,
             keep_failed: bool = False) -> dict[str, Any]:
        from aether_core.healer import Healer, HealError

        healer = Healer(self)
        try:
            if not execute:
                return healer.prepare(command)
            return healer.execute(command, max_attempts=max_attempts, llm=llm, keep_failed=keep_failed)
        except HealError as exc:
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------ v0.2 · living rules & docs
    def rules_facts(self) -> dict[str, Any]:
        from aether_core.docs_gen import endpoints, module_graph

        pol = self.policy
        graph = module_graph(self.db)
        scars = [s.to_dict() for s in self.db.list_scars(500) if s.severity in {"critical", "high"}]
        return {
            "project": self.repo_name,
            "linked_repos": [r["name"] for r in self.config.linked_repos],
            "index_version": self.db.index_version(),
            "counts": {
                **self.db.counts(),
                "code_files": sum(m["files"] for m in graph["modules"]),
                "code_symbols": sum(m["symbols"] for m in graph["modules"]),
                "code_edges": int(self.db.scalar(
                    "SELECT COUNT(*) FROM edges e JOIN files a ON a.id = e.src_file_id JOIN files b ON b.id = e.dst_file_id "
                    "WHERE a.language NOT IN ('markdown', 'text') AND b.language NOT IN ('markdown', 'text')") or 0),
            },
            "modules": graph["modules"],
            "module_edges": graph["edges"],
            "routes": [e for e in endpoints(self.db) if pol.can_read(e["file"])],
            "decisions": self.db.list_decisions(),
            "scars": [s for s in scars if pol.can_read(s["file_path"])],
            "hotspots": [h.to_dict() for h in self.hotspot_map()[:5]],
        }

    def sync_rules(self, targets: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
        from aether_core.context_engine import estimate_tokens
        from aether_core.rules_sync import DEFAULT_TARGETS, render_rules, upsert_block, write_rules

        body = self.vault.mask(render_rules(self.rules_facts())).text
        names = list(targets or DEFAULT_TARGETS)
        if dry_run:
            files = [{"path": t, "action": "dry-run"} for t in names]
        else:
            files = write_rules(self.root, body, names, self.policy.can_write)
            self.bus.publish("RULES_SYNCED", {"files": [f["path"] for f in files if f["action"] in {"created", "updated"}],
                                              "tokens": estimate_tokens(body)})
        return {"ok": True, "files": files, "tokens": estimate_tokens(body), "block": upsert_block("", body)}

    def generate_docs(self, out: str | None = "docs/AETHER_DOCS.md", write: bool = True) -> dict[str, Any]:
        from aether_core.docs_gen import render_docs

        result = render_docs(self.db, self.repo_name, self.db.index_version())
        result["markdown"] = self.vault.mask(result["markdown"]).text
        result["path"] = None
        if write and out:
            rel = normalize_rel_path(out)
            target = (self.root / rel).resolve()
            if self.root not in target.parents:
                return {"ok": False, "error": "output must be inside the project"}
            if not self.policy.can_write(rel):
                self.bus.publish("POLICY_BLOCKED", {"action": "docs", "file": rel})
                return {"ok": False, "error": f"{rel} is read-only or denied by .aether/policy.json"}
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(result["markdown"], encoding="utf-8")
            result["path"] = rel
            self.bus.publish("DOCS_GENERATED", {"file": rel, "modules": result["modules"], "endpoints": result["endpoints"]},
                             [f"{self.repo_name}:{rel}"])
        result["ok"] = True
        return result

    # ------------------------------------------------------------------ v0.2 · share card
    def share_metrics(self) -> dict[str, Any]:
        totals = self.db.telemetry_totals()
        pct = (totals["saved_tokens"] / totals["raw_tokens"] * 100.0) if totals["raw_tokens"] else 0.0
        return {
            "project": self.repo_name, "version": __version__, "files_indexed": self.db.counts()["files"],
            "tokens_saved": totals["saved_tokens"], "savings_pct": round(pct, 2), "usd_saved": totals["saved_usd"],
            "p50_ms": self.db.telemetry_latency_percentiles()["p50"], "queries": totals["queries"],
            "series": [r["saved_tokens"] for r in self.db.telemetry_series(40)],
        }

    def share_card(self, out: Path | str | None = None) -> dict[str, Any]:
        from aether_core.share_card import render_share_card

        metrics = self.share_metrics()
        svg = render_share_card(metrics)
        target = Path(out) if out else self.aether_dir / "share" / "aether-card.svg"
        target = (Path.cwd() / target).resolve() if not target.is_absolute() else target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(svg, encoding="utf-8")
        return {"ok": True, "path": str(target), "bytes": len(svg.encode("utf-8")), "metrics": metrics}

    # ------------------------------------------------------------------ v0.2 · hotspots
    def hotspot_map(self, commits: int = 500) -> list[Any]:
        from aether_core.hotspots import compute_hotspots, git_head

        roots = self.repo_roots()
        key = (self.db.index_version(), self.db.counts()["scars"], commits, tuple(git_head(r) for r in roots.values()))
        if self._hotspot_cache is not None and self._hotspot_cache[0] == key:
            return self._hotspot_cache[1]
        pol = self.policy
        spots = [h for h in compute_hotspots(self.db, roots, commits) if pol.can_read(h.file)]
        self._hotspot_cache = (key, spots)
        return spots

    def get_code_hotspots(self, limit: int = 15, commits: int = 500) -> dict[str, Any]:
        spots = self.hotspot_map(commits)
        top = [h.to_dict() for h in spots[: max(1, int(limit))]]
        levels: dict[str, int] = {}
        for h in spots:
            levels[h.level] = levels.get(h.level, 0) + 1
        self.bus.publish("ANALYSIS", {"kind": "hotspots", "files": len(spots), "top": top[0]["file"] if top else ""},
                         [f"{h['repo']}:{h['file']}" for h in top[:12]])
        return {"hotspots": top, "files_analyzed": len(spots), "by_level": levels,
                "weights": {"complexity": 0.30, "fan_in": 0.25, "churn": 0.25, "scars": 0.20}, "commits_considered": commits}

    # ------------------------------------------------------------------ v0.2 · episodic memory
    def save_session_checkpoint(self, goal: str, files_touched: list[str] | None = None, pending: list[str] | None = None,
                                decisions: list[str] | None = None, notes: str = "", auto_files: bool = True) -> dict[str, Any]:
        from aether_core.sessions import MAX_CHECKPOINT_TOKENS, build_checkpoint, git_touched_files

        if not goal.strip():
            return {"ok": False, "error": "goal is required"}
        files = [normalize_rel_path(f) for f in (files_touched or [])]
        if not files and auto_files:
            files = git_touched_files(self.root)
        pol = self.policy
        files = [f for f in files if pol.can_read(f)]
        summary, tokens, kept = build_checkpoint(
            self.vault.mask(goal).text, files, [self.vault.mask(p).text for p in pending or []],
            [self.vault.mask(d).text for d in decisions or []], self.vault.mask(notes).text, MAX_CHECKPOINT_TOKENS)
        cid = self.db.add_checkpoint(self.budget.session, str(kept["goal"]), list(kept["files"]), list(kept["pending"]),
                                     list(kept["decisions"]), str(kept["notes"]), summary, tokens)
        self.bus.publish("CHECKPOINT_SAVED", {"id": cid, "goal": str(kept["goal"])[:120], "tokens": tokens,
                                              "files": len(kept["files"])}, [f"{self.repo_name}:{f}" for f in kept["files"]][:30])
        return {"ok": True, "id": cid, "tokens": tokens, "summary": summary, "files": kept["files"]}

    def resume_session(self, checkpoint_id: int | None = None) -> dict[str, Any]:
        cp = self.db.get_checkpoint(checkpoint_id if checkpoint_id else None)
        if cp is None:
            return {"ok": False, "error": "no checkpoint found" if not checkpoint_id else f"checkpoint {checkpoint_id} not found"}
        scars = []
        for f in cp["files"][:10]:
            scars += [{"file": s.file_path, "severity": s.severity, "error": s.error_summary}
                      for s in self.db.scars_for_path(f, record_hit=False) if s.severity in {"critical", "high"}]
        masked, _ = self.vault.mask_obj({**cp, "ok": True, "age_s": round(time.time() - cp["created_at"], 1),
                                         "scars_on_files": scars[:8]})
        return masked

    # ------------------------------------------------------------------ v0.2 · code intelligence
    def semantic_diff(self, ref: str = "HEAD", paths: list[str] | None = None, compact: bool = False,
                      max_files: int | None = None) -> dict[str, Any]:
        """Symbol-level diff. ``compact`` (used for agents) truncates signatures and turns body-only
        changes into counts; ``max_files`` caps the per-file section (the summary always covers everything)."""
        from aether_core.semantic_diff import SemanticDiffError, semantic_diff

        try:
            result = semantic_diff(self.root, ref, paths)
        except SemanticDiffError as exc:
            return {"ok": False, "error": str(exc)}
        pol = self.policy
        result["files"] = [f for f in result["files"] if pol.can_read(f["path"])]
        result["breaking"] = [b for b in result["breaking"] if pol.can_read(b["file"])]
        if compact:
            def clip(text: str) -> str:
                return text if len(text) <= 160 else text[:159] + "…"

            for f in result["files"]:
                f["modified"] = len(f["modified"])
                if len(f["added"]) > 20:
                    f["added_not_listed"] = len(f["added"]) - 20
                f["added"] = [{"kind": x["kind"], "symbol": x["symbol"]} for x in f["added"][:20]]
                for item in f["removed"]:
                    item["signature"] = clip(item.get("signature", ""))
                for item in f["signature_changed"]:
                    item["before"], item["after"] = clip(item["before"]), clip(item["after"])
            for b in result["breaking"]:
                for key in ("before", "after"):
                    if key in b:
                        b[key] = clip(b[key])
        if max_files is not None and len(result["files"]) > max_files:
            result["files_truncated"] = len(result["files"]) - max_files
            result["files"] = result["files"][:max_files]
        masked, _ = self.vault.mask_obj(result)
        masked["ok"] = True
        self.bus.publish("ANALYSIS", {"kind": "semantic_diff", "ref": ref, **result["summary"],
                                      "breaking": len(result["breaking"])},
                         [f"{self.repo_name}:{f['path']}" for f in result["files"][:30]])
        return masked

    def find_dead_code(self, min_confidence: str = "medium", limit: int = 200) -> dict[str, Any]:
        from aether_core.dead_code import find_dead_code

        result = find_dead_code(self.db, self.repo_roots(), min_confidence, limit)
        pol = self.policy
        result["dead_symbols"] = [d for d in result["dead_symbols"] if pol.can_read(d["file"])]
        masked, _ = self.vault.mask_obj(result)
        self.bus.publish("ANALYSIS", {"kind": "dead_code", "symbols": result["total"],
                                      "lines": result["dead_lines_estimate"]},
                         sorted({f"{d['repo']}:{d['file']}" for d in result["dead_symbols"][:30]}))
        return masked

    def generate_tests(self, file_path: str, out: str | None = None, write: bool = True, force: bool = False) -> dict[str, Any]:
        from aether_core.test_gen import TestGenError, generate

        rel = normalize_rel_path(file_path)
        if not self.policy.can_read(rel):
            return {"ok": False, "error": f"{rel} is denied by .aether/policy.json"}
        try:
            result = generate(self.root, rel, out)
        except TestGenError as exc:
            return {"ok": False, "error": str(exc)}
        result["ok"] = True
        result["written"] = False
        if write:
            target = (self.root / result["output"]).resolve()
            if self.root not in target.parents:
                return {"ok": False, "error": "output must be inside the project"}
            if not self.policy.can_write(result["output"]):
                return {"ok": False, "error": f"{result['output']} is read-only or denied by .aether/policy.json"}
            if target.exists() and not force:
                return {**result, "ok": False, "error": f"{result['output']} exists (use --force to overwrite)"}
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(result["content"], encoding="utf-8")
            result["written"] = True
        return result

    # ------------------------------------------------------------------ v0.2 · context budget
    def record_budget(self, tool: str, response: Any, query: str = "", files: list[str] | None = None) -> dict[str, Any]:
        return self.budget.record(tool, response, query=query, files=files)

    def budget_report(self, session: str | None = None, all_sessions: bool = False) -> dict[str, Any]:
        report = self.budget.report(session, all_sessions)
        masked, _ = self.vault.mask_obj(report)
        return masked

    def set_budget_threshold(self, tokens: int) -> int:
        self.config.budget_tokens = max(1000, int(tokens))
        self.config.save(self.config_path)
        self.budget.threshold = self.config.budget_tokens
        return self.config.budget_tokens

    # ------------------------------------------------------------------ team brain
    @property
    def team_brain_path(self) -> Path:
        return self.aether_dir / TEAM_BRAIN_FILENAME

    def export_brain(self, path: Path | None = None) -> dict[str, Any]:
        return export_team_brain(self.db, path or self.team_brain_path, self.bus)

    def import_brain(self, path: Path | None = None) -> dict[str, Any]:
        return import_team_brain(self.db, path or self.team_brain_path, self.bus)

    def close(self) -> None:
        self.db.close()
