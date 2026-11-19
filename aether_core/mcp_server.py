"""FastMCP server exposing the Aether kernel to Claude Code, Cursor, Windsurf and Aider.

Run with ``aether mcp`` (stdio). Every tool output passes through the secret firewall before it
reaches the agent, and every call is broadcast on the event bus so the 3D dashboard animates it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from aether_core import __version__
from aether_core.kernel import AetherKernel

INSTRUCTIONS = """AetherOS is a local-first memory kernel for this codebase.
Workflow that saves tokens and prevents regressions:
1. Call `query_smart_context` BEFORE opening large files — it returns signatures, exact fragments,
   dependencies and known scars within a token budget.
2. Call `check_scars` on a file BEFORE editing it, and `impact_analysis` before changing public APIs.
3. Call `create_snapshot` before risky multi-file edits; `rollback_snapshot` restores the exact state.
4. After fixing a bug, call `record_scar` so the team (and future you) never repeat it.
5. Tests failing? `prepare_heal_context` returns frames, scars and the relevant code in one call.
6. Review your work with `semantic_diff`; find risky files with `get_code_hotspots`, orphans with `find_dead_code`.
7. Before the context window fills up, `save_session_checkpoint`; a new session calls `resume_session`.
8. `check_path_policy` tells you whether a path is read-only or denied by .aether/policy.json.
Secrets in outputs appear as <<AETHER_MASKED_SECRET_N>>; never try to guess their values."""


def build_server(kernel: AetherKernel | None = None, root: Path | str | None = None) -> FastMCP:
    k = kernel or AetherKernel(root)
    mcp = FastMCP("aether-os", instructions=INSTRUCTIONS, version=__version__)

    def guard(obj: Any, tool: str = "", query: str = "") -> Any:
        masked, findings = k.vault.mask_obj(obj)
        if findings:
            for f in findings:
                k.db.add_security_finding("secret_masked", f.rule, f.severity, "", f.line, f.preview)
            k.bus.publish("SECRET_BLOCKED", {"count": len(findings), "rules": sorted({f.rule for f in findings}),
                                             "context": "mcp_output"})
        if tool:
            k.record_budget(tool, masked, query=query)  # tokens the agent actually receives
        return masked

    @mcp.tool
    def query_smart_context(query: str, max_tokens: int = 1500) -> dict[str, Any]:
        """Hybrid (BM25 + vector) search over the multi-repo code graph. Returns a compressed context
        package: relevant signatures/docstrings, exact matching code fragments, direct dependencies and
        dependents, scars (past bugs) for the files involved, and token/USD savings metrics."""
        return guard(k.query_smart_context(query, max_tokens=max(200, min(int(max_tokens), 20000))),
                     "query_smart_context", query)

    @mcp.tool
    def impact_analysis(file_path: str) -> dict[str, Any]:
        """List files, API endpoints and linked repositories that depend on `file_path` (transitively)
        and could break if it changes, with a risk score and recommendation."""
        return guard(k.impact_analysis(file_path), "impact_analysis", file_path)

    @mcp.tool
    def record_scar(file_path: str, error_summary: str, fix_applied: str, severity: str = "high") -> dict[str, Any]:
        """Record a lesson learned after fixing a bug (file, what went wrong, how it was fixed).
        Severity: critical | high | medium | low. Scars are injected as warnings in future queries."""
        return guard(k.record_scar(file_path, error_summary, fix_applied, severity), "record_scar")

    @mcp.tool
    def check_scars(file_path: str) -> list[dict[str, Any]]:
        """Return historical warnings (scars) for a file before editing it, most severe first."""
        return guard(k.check_scars(file_path), "check_scars", file_path)

    @mcp.tool
    def create_snapshot(label: str) -> dict[str, Any]:
        """Take an instant time-travel snapshot of every tracked file (content-addressed, deduplicated)."""
        return guard(k.create_snapshot(label), "create_snapshot")

    @mcp.tool
    def rollback_snapshot(snapshot_id: str) -> dict[str, Any]:
        """Restore the project to an exact snapshot. A safety snapshot of the current state is taken first."""
        try:
            return guard(k.rollback_snapshot(snapshot_id), "rollback_snapshot")
        except (FileNotFoundError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def scan_security(target_path: str = ".") -> dict[str, Any]:
        """Audit a path for leaked secrets (AWS, OpenAI, Anthropic, GitHub, Stripe, JWT, private keys,
        high-entropy strings) and prompt-injection payloads. Secret values are never returned."""
        return guard(k.scan_security(target_path), "scan_security")

    @mcp.tool
    def get_telemetry_stats() -> dict[str, Any]:
        """Tokens and USD saved, files indexed, graph size, security counters and swarm status."""
        return guard(k.get_telemetry_stats(), "get_telemetry_stats")

    # ------------------------------------------------------------------ v0.2 tools
    @mcp.tool
    def prepare_heal_context(command: str = "", output: str = "", max_tokens: int = 1500) -> dict[str, Any]:
        """Auto-fix loop, step 1. Runs `command` (only test/build runners allowed by .aether/policy.json)
        or analyses a failing `output` you already have. Returns the error lines, the project stack frames
        with code windows and enclosing symbols, scars on those files and the most relevant code context.
        Also written to .aether/last_heal_context.json. Never edits files."""
        return guard(k.prepare_heal_context(command, output if output else None, max(300, min(int(max_tokens), 8000))),
                     "prepare_heal_context", command or output[:200])

    @mcp.tool
    def get_code_hotspots(limit: int = 15) -> dict[str, Any]:
        """Technical-debt hotspots: files ranked by a 0-100 risk score combining symbol complexity,
        incoming dependencies, git churn and recorded scars, with the reasons for each score."""
        return guard(k.get_code_hotspots(max(1, min(int(limit), 100))), "get_code_hotspots")

    @mcp.tool
    def save_session_checkpoint(goal: str, files_touched: list[str] | None = None, pending: list[str] | None = None,
                                decisions: list[str] | None = None, notes: str = "") -> dict[str, Any]:
        """Persist the current task state (goal, touched files, pending steps, decisions) compressed to
        <400 tokens so a fresh session can continue with `resume_session`. Touched files default to git status."""
        return guard(k.save_session_checkpoint(goal, files_touched, pending, decisions, notes), "save_session_checkpoint")

    @mcp.tool
    def resume_session(checkpoint_id: int = 0) -> dict[str, Any]:
        """Load the latest (or a specific) session checkpoint plus high-severity scars on its files."""
        return guard(k.resume_session(checkpoint_id or None), "resume_session")

    @mcp.tool
    def semantic_diff(ref: str = "HEAD", paths: list[str] | None = None, max_files: int = 25) -> dict[str, Any]:
        """Symbol-level diff of the working tree against a git ref: functions/classes/methods/routes added,
        removed, renamed, signature-changed (flagged as potentially breaking); body-only edits are counted.
        Compact by design (signatures clipped, at most `max_files` files); pass `paths` to zoom in."""
        return guard(k.semantic_diff(ref, paths or None, compact=True, max_files=max(1, min(int(max_files), 200))),
                     "semantic_diff", ref)

    @mcp.tool
    def find_dead_code(min_confidence: str = "medium", limit: int = 100) -> dict[str, Any]:
        """Functions, classes and methods with zero incoming references (graph + textual cross-check),
        excluding route handlers, registered callbacks, tests and CLI entrypoints; plus HTTP routes no
        linked client consumes. min_confidence: high | medium | low."""
        conf = min_confidence if min_confidence in {"high", "medium", "low"} else "medium"
        return guard(k.find_dead_code(conf, max(1, min(int(limit), 500))), "find_dead_code")

    @mcp.tool
    def check_path_policy(path: str) -> dict[str, Any]:
        """Whether `path` may be read/modified under .aether/policy.json (deny / read_only rules)."""
        return guard(k.check_path_policy(path), "check_path_policy", path)

    @mcp.tool
    def get_context_budget() -> dict[str, Any]:
        """Tokens consumed by this session's Aether tool responses vs. the configured budget, per tool,
        per file and per query. Use it to decide when to checkpoint and start a fresh context."""
        return guard(k.budget_report(k.budget.session), "get_context_budget")

    @mcp.resource("aether://telemetry", mime_type="application/json")
    def telemetry_resource() -> dict[str, Any]:
        """Live telemetry snapshot."""
        return k.get_telemetry_stats()

    @mcp.resource("aether://scars", mime_type="application/json")
    def scars_resource() -> list[dict[str, Any]]:
        """All recorded scars, newest first."""
        return guard([s.to_dict() for s in k.db.list_scars(200)])

    @mcp.resource("aether://graph/summary", mime_type="application/json")
    def graph_summary_resource() -> dict[str, Any]:
        """Repositories, counts and HTTP routes in the code graph."""
        routes = [
            {"repo": n.repo_name, "file": n.path, "method": n.metadata.get("method"), "path": n.metadata.get("path")}
            for n in k.db.nodes_by_kind("route")[:300]
        ]
        return {"repos": k.db.list_repos(), "counts": k.db.counts(), "routes": routes}

    @mcp.resource("aether://decisions", mime_type="application/json")
    def decisions_resource() -> list[dict[str, Any]]:
        """Architecture decisions shared through the team brain."""
        return guard(k.db.list_decisions())

    @mcp.resource("aether://policy", mime_type="application/json")
    def policy_resource() -> dict[str, Any]:
        """Active permission policy (deny, read_only, allowed_commands)."""
        return {**k.policy.to_dict(), "source": k.policy.source}

    @mcp.resource("aether://session/latest", mime_type="application/json")
    def latest_checkpoint_resource() -> dict[str, Any]:
        """Most recent session checkpoint."""
        return guard(k.resume_session(None))

    mcp.aether_kernel = k  # type: ignore[attr-defined]
    return mcp


def run_stdio(root: Path | str | None = None, auto_index: bool = True) -> None:
    kernel = AetherKernel(root)
    if auto_index and kernel.db.counts()["files"] == 0:
        kernel.index()
    server = build_server(kernel)
    server.run(show_banner=False) if _supports_banner_flag(server) else server.run()


def _supports_banner_flag(server: FastMCP) -> bool:
    import inspect

    try:
        return "show_banner" in inspect.signature(server.run).parameters
    except (TypeError, ValueError):
        return False
