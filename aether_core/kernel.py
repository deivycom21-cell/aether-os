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
from aether_core.context_engine import ContextEngine
from aether_core.database import AetherDB, normalize_severity
from aether_core.event_bus import EventBus
from aether_core.git_sync import TEAM_BRAIN_FILENAME, SnapshotManager, export_team_brain, import_team_brain
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
        self._index_lock = threading.Lock()
        self.started_at = time.time()

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

    def rollback_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        result = self.snapshots.rollback(snapshot_id)
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
            "uptime_s": round(time.time() - self.started_at, 1),
        }

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
