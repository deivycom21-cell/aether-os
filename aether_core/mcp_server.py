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
Secrets in outputs appear as <<AETHER_MASKED_SECRET_N>>; never try to guess their values."""


def build_server(kernel: AetherKernel | None = None, root: Path | str | None = None) -> FastMCP:
    k = kernel or AetherKernel(root)
    mcp = FastMCP("aether-os", instructions=INSTRUCTIONS, version=__version__)

    def guard(obj: Any) -> Any:
        masked, findings = k.vault.mask_obj(obj)
        if findings:
            for f in findings:
                k.db.add_security_finding("secret_masked", f.rule, f.severity, "", f.line, f.preview)
            k.bus.publish("SECRET_BLOCKED", {"count": len(findings), "rules": sorted({f.rule for f in findings}),
                                             "context": "mcp_output"})
        return masked

    @mcp.tool
    def query_smart_context(query: str, max_tokens: int = 1500) -> dict[str, Any]:
        """Hybrid (BM25 + vector) search over the multi-repo code graph. Returns a compressed context
        package: relevant signatures/docstrings, exact matching code fragments, direct dependencies and
        dependents, scars (past bugs) for the files involved, and token/USD savings metrics."""
        return guard(k.query_smart_context(query, max_tokens=max(200, min(int(max_tokens), 20000))))

    @mcp.tool
    def impact_analysis(file_path: str) -> dict[str, Any]:
        """List files, API endpoints and linked repositories that depend on `file_path` (transitively)
        and could break if it changes, with a risk score and recommendation."""
        return guard(k.impact_analysis(file_path))

    @mcp.tool
    def record_scar(file_path: str, error_summary: str, fix_applied: str, severity: str = "high") -> dict[str, Any]:
        """Record a lesson learned after fixing a bug (file, what went wrong, how it was fixed).
        Severity: critical | high | medium | low. Scars are injected as warnings in future queries."""
        return guard(k.record_scar(file_path, error_summary, fix_applied, severity))

    @mcp.tool
    def check_scars(file_path: str) -> list[dict[str, Any]]:
        """Return historical warnings (scars) for a file before editing it, most severe first."""
        return guard(k.check_scars(file_path))

    @mcp.tool
    def create_snapshot(label: str) -> dict[str, Any]:
        """Take an instant time-travel snapshot of every tracked file (content-addressed, deduplicated)."""
        return guard(k.create_snapshot(label))

    @mcp.tool
    def rollback_snapshot(snapshot_id: str) -> dict[str, Any]:
        """Restore the project to an exact snapshot. A safety snapshot of the current state is taken first."""
        try:
            return guard(k.rollback_snapshot(snapshot_id))
        except (FileNotFoundError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool
    def scan_security(target_path: str = ".") -> dict[str, Any]:
        """Audit a path for leaked secrets (AWS, OpenAI, Anthropic, GitHub, Stripe, JWT, private keys,
        high-entropy strings) and prompt-injection payloads. Secret values are never returned."""
        return k.scan_security(target_path)

    @mcp.tool
    def get_telemetry_stats() -> dict[str, Any]:
        """Tokens and USD saved, files indexed, graph size, security counters and swarm status."""
        return k.get_telemetry_stats()

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
