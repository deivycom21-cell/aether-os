"""End-to-end MCP tests through FastMCP's in-memory client (the same protocol Claude Code speaks)."""

from __future__ import annotations

import json
from typing import Any

from fastmcp import Client

from aether_core.kernel import AetherKernel
from aether_core.mcp_server import build_server
from tests.conftest import fake_secret

EXPECTED_TOOLS = {
    "query_smart_context", "impact_analysis", "record_scar", "check_scars", "create_snapshot",
    "rollback_snapshot", "scan_security", "get_telemetry_stats",
}


async def call(client: Client, name: str, args: dict[str, Any] | None = None) -> Any:
    result = await client.call_tool(name, args or {})
    assert not result.is_error, result
    return result.data


async def test_tools_and_resources_are_registered(kernel: AetherKernel) -> None:
    async with Client(build_server(kernel)) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert EXPECTED_TOOLS <= set(tools)
        assert "max_tokens" in tools["query_smart_context"].inputSchema["properties"]
        uris = {str(r.uri) for r in await client.list_resources()}
        assert {"aether://telemetry", "aether://scars", "aether://graph/summary", "aether://decisions"} <= uris


async def test_query_smart_context_returns_compressed_package(kernel: AetherKernel) -> None:
    async with Client(build_server(kernel)) as client:
        pkg = await call(client, "query_smart_context", {"query": "charge customer payment amount", "max_tokens": 1200})
    assert pkg["results"], pkg
    top_symbols = [r["symbol"] for r in pkg["results"][:3]]
    assert any(s.endswith("charge_customer") for s in top_symbols)
    m = pkg["metrics"]
    assert m["compressed_tokens"] <= 1200 * 1.15
    assert m["saved_tokens"] == max(0, m["raw_tokens"] - m["compressed_tokens"]) and set(m["saved_usd"]) == {"sonnet", "opus"}
    assert any(rel["file"] == "app/payments.py" for rel in pkg["relations"])
    stats = kernel.get_telemetry_stats()
    assert stats["queries"] == 1 and stats["tokens_saved"] == m["saved_tokens"]


async def test_large_files_are_compressed_aggressively(kernel: AetherKernel) -> None:
    """A 2,000-line module: the agent should receive the one relevant function, not the file."""
    filler = "\n\n".join(
        f"def helper_{i}(value: int) -> int:\n    \"\"\"Arithmetic helper number {i}.\"\"\"\n    return value * {i} + {i % 7}"
        for i in range(500)
    )
    target = ("\n\ndef reconcile_ledger_balances(ledger: dict, statement: dict) -> list:\n"
              "    \"\"\"Reconcile ledger balances against a bank statement and report mismatches.\"\"\"\n"
              "    return [k for k in ledger if ledger[k] != statement.get(k)]\n")
    (kernel.root / "app" / "ledger.py").write_text('"""Ledger utilities."""\n\n' + filler + target, encoding="utf-8")
    kernel.index(run_plugins=False)
    async with Client(build_server(kernel)) as client:
        pkg = await call(client, "query_smart_context", {"query": "reconcile ledger balances statement", "max_tokens": 1500})
    assert pkg["results"][0]["symbol"] == "app.ledger.reconcile_ledger_balances"
    m = pkg["metrics"]
    assert m["raw_tokens"] > 10_000 and m["savings_pct"] > 85, m
    assert m["saved_usd"]["opus"] > m["saved_usd"]["sonnet"] > 0


async def test_budget_is_respected_for_tiny_budgets(kernel: AetherKernel) -> None:
    async with Client(build_server(kernel)) as client:
        pkg = await call(client, "query_smart_context", {"query": "user", "max_tokens": 200})
    assert pkg["metrics"]["compressed_tokens"] < 600


async def test_impact_analysis_crosses_repositories(kernel: AetherKernel) -> None:
    async with Client(build_server(kernel)) as client:
        r = await call(client, "impact_analysis", {"file_path": "app/api.py"})
        missing = await call(client, "impact_analysis", {"file_path": "does/not/exist.py"})
    assert r["found"] and "frontend" in r["linked_repos_affected"]
    assert {e["path"] for e in r["endpoints_defined"]} == {"/api/users/{user_id}", "/api/payments", "/health"}
    assert any(c["repo"] == "frontend" and c["file"] == "src/api/client.ts" for c in r["endpoint_consumers"])
    assert r["risk_level"] in {"medium", "high", "critical"}
    assert missing["found"] is False


async def test_scars_roundtrip_and_injection_into_context(kernel: AetherKernel) -> None:
    async with Client(build_server(kernel)) as client:
        rec = await call(client, "record_scar", {
            "file_path": "app/payments.py", "error_summary": "Double charge when the gateway times out",
            "fix_applied": "Idempotency key per invoice", "severity": "critical"})
        again = await call(client, "record_scar", {
            "file_path": "app/payments.py", "error_summary": "Double charge when the gateway times out",
            "fix_applied": "Idempotency key per invoice", "severity": "critical"})
        scars = await call(client, "check_scars", {"file_path": "app/payments.py"})
        pkg = await call(client, "query_smart_context", {"query": "charge customer"})
    assert rec["ok"] and rec["created"] and not again["created"] and rec["id"] == again["id"]
    assert len(scars) == 1 and scars[0]["severity"] == "critical"
    assert pkg["scars"] and pkg["scars"][0]["error"].startswith("Double charge")
    assert pkg["warnings"][0].startswith("⚠")


async def test_snapshot_and_rollback_via_mcp(kernel: AetherKernel) -> None:
    target = kernel.root / "app" / "payments.py"
    original = target.read_text(encoding="utf-8")
    async with Client(build_server(kernel)) as client:
        snap = await call(client, "create_snapshot", {"label": "before refactor"})
        target.write_text("BROKEN = True\n", encoding="utf-8")
        (kernel.root / "app" / "scratch.py").write_text("x = 1\n", encoding="utf-8")
        result = await call(client, "rollback_snapshot", {"snapshot_id": snap["id"]})
        bad = await call(client, "rollback_snapshot", {"snapshot_id": "../../etc"})
    assert target.read_text(encoding="utf-8") == original
    assert not (kernel.root / "app" / "scratch.py").exists()
    assert result["restored"] == 1 and result["deleted_new_files"] == 1 and result["safety_snapshot"]
    assert bad["ok"] is False


async def test_scan_security_and_output_masking(kernel: AetherKernel) -> None:
    key = fake_secret("ghp_", 36)
    (kernel.root / "app" / "leak.py").write_text(f'"""Leaky module."""\nGITHUB_TOKEN = "{key}"\n', encoding="utf-8")
    kernel.index(run_plugins=False)
    async with Client(build_server(kernel)) as client:
        report = await call(client, "scan_security", {"target_path": "."})
        pkg = await call(client, "query_smart_context", {"query": "leaky module github token"})
        stats = await call(client, "get_telemetry_stats")
        res = await client.read_resource("aether://graph/summary")
    assert report["secret_count"] == 1 and report["secrets"][0]["file"] == "app/leak.py"
    assert key not in json.dumps(pkg)
    assert stats["secrets_blocked"] >= 2 and stats["files_indexed"] >= 10
    summary = json.loads(res[0].text)
    assert summary["counts"]["routes"] >= 4 and {r["name"] for r in summary["repos"]} == {"backend", "frontend"}


async def test_events_are_published_for_dashboard(kernel: AetherKernel) -> None:
    seen: list[str] = []
    kernel.bus.add_listener(lambda ev: seen.append(ev.type))
    async with Client(build_server(kernel)) as client:
        await call(client, "query_smart_context", {"query": "user repository"})
        await call(client, "create_snapshot", {"label": "x"})
        await call(client, "impact_analysis", {"file_path": "app/models.py"})
    assert {"CONTEXT_QUERY", "SNAPSHOT_CREATED", "IMPACT_ANALYSIS"} <= set(seen)
    persisted = {e["type"] for e in kernel.db.recent_events(50)}
    assert {"CONTEXT_QUERY", "SNAPSHOT_CREATED", "IMPACT_ANALYSIS", "INDEX_UPDATED"} <= persisted
