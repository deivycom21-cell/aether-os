"""v0.2 · #4 share card, #7 episodic checkpoints, #13 permission policy, #15 context budget."""

from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient
from fastmcp import Client

from aether_core.budget import BudgetTracker, files_in
from aether_core.context_engine import estimate_tokens
from aether_core.kernel import AetherKernel
from aether_core.mcp_server import build_server
from aether_core.policy import Policy, command_tokens, glob_match
from aether_core.sessions import MAX_CHECKPOINT_TOKENS, build_checkpoint
from aether_core.share_card import human_int, human_usd, render_share_card
from aether_web.server import create_app
from tests.conftest import fake_secret

# ---------------------------------------------------------------------------- episodic memory


def test_checkpoint_compression_stays_under_400_tokens() -> None:
    files = [f"src/module_{i}/very/long/path/component_{i}.py" for i in range(40)]
    pending = [f"Refactor the payment reconciliation step {i} so that every ledger entry is idempotent" for i in range(20)]
    decisions = [f"Decision {i}: keep FastAPI routers per bounded context and share DTOs via a schema package" for i in range(15)]
    summary, tokens, kept = build_checkpoint("Ship multi-currency invoicing " * 10, files, pending, decisions, "notes " * 400)
    assert tokens <= MAX_CHECKPOINT_TOKENS == 400 and tokens == estimate_tokens(summary)
    assert summary.startswith("GOAL: Ship multi-currency invoicing") and "PENDING:" in summary and kept["pending"]
    small, small_tokens, _ = build_checkpoint("Fix login", ["app/auth.py"], ["add test"], [], "")
    assert small == "GOAL: Fix login\nFILES: app/auth.py\nPENDING:\n- add test" and small_tokens < 30


def test_checkpoint_roundtrip_with_git_touched_files(git_kernel: AetherKernel) -> None:
    (git_kernel.root / "app" / "payments.py").write_text("# wip\n", encoding="utf-8")
    (git_kernel.root / "app" / "refunds.py").write_text("x = 1\n", encoding="utf-8")
    git_kernel.record_scar("app/payments.py", "Double charge on retry", "Idempotency key", "critical")
    secret = fake_secret("sk-ant-api03-", 40)
    saved = git_kernel.save_session_checkpoint("Add refunds", pending=["wire refund route"],
                                               decisions=["refunds reuse charge ids"], notes=f"key {secret} rotated")
    assert saved["ok"] and set(saved["files"]) == {"app/payments.py", "app/refunds.py"} and saved["tokens"] < 400
    assert secret not in json.dumps(saved)
    r = git_kernel.resume_session()
    assert r["ok"] and r["id"] == saved["id"] and r["goal"] == "Add refunds" and r["pending"] == ["wire refund route"]
    assert r["scars_on_files"][0]["error"] == "Double charge on retry"
    assert secret not in json.dumps(r)
    assert git_kernel.resume_session(9999)["ok"] is False
    assert git_kernel.save_session_checkpoint("  ")["ok"] is False
    assert git_kernel.db.counts()["checkpoints"] == 1


async def test_checkpoint_tools_over_mcp(kernel: AetherKernel) -> None:
    async with Client(build_server(kernel)) as client:
        saved = await client.call_tool("save_session_checkpoint", {
            "goal": "Paginate users", "files_touched": ["app/api.py"], "pending": ["cursor param"], "decisions": []})
        resumed = await client.call_tool("resume_session", {})
        latest = await client.read_resource("aether://session/latest")
    assert saved.data["ok"] and resumed.data["files"] == ["app/api.py"]
    assert json.loads(latest[0].text)["goal"] == "Paginate users"


# ---------------------------------------------------------------------------- policy


def test_policy_glob_and_command_matching() -> None:
    assert glob_match("secrets/prod/key.txt", "secrets/**") and glob_match("secrets/prod/key.txt", "secrets/")
    assert glob_match("deep/dir/id_rsa", "id_rsa*") and glob_match("infra\\prod\\main.tf", "infra/prod")
    assert not glob_match("src/secretsauce.py", "secrets/**") and glob_match("a/b.pem", "*.pem")
    p = Policy()
    assert p.command_allowed(["pytest", "-x"]) and p.command_allowed(["C:\\Python\\python.exe", "-m", "pytest"])
    assert p.command_allowed(["npm.cmd", "test"]) and not p.command_allowed(["npm", "publish"])
    assert not p.command_allowed(["rm", "-rf", "/"]) and Policy(allowed_commands=["*"]).command_allowed(["anything"])
    assert command_tokens(["python3.12", "-m", "pytest"])[0] == "python"
    assert p.is_denied(".env") and not p.is_denied(".env.example") and p.can_write("app/x.py")


def test_policy_hides_denied_files_and_blocks_writes(kernel: AetherKernel) -> None:
    before = kernel.query_smart_context("charge customer payment")
    assert any(r["file"] == "app/payments.py" for r in before["results"])
    kernel.save_policy(Policy(deny=["app/payments.py"], read_only=["app/models.py"]))
    after = kernel.query_smart_context("charge customer payment")
    assert all(r["file"] != "app/payments.py" for r in after["results"])
    assert any("hidden by .aether/policy.json" in w for w in after["warnings"])
    assert all("app/payments.py" not in rel["depends_on"] + rel["used_by"] for rel in after["relations"])
    assert kernel.impact_analysis("app/payments.py")["found"] is False
    snap = kernel.create_snapshot("before")
    (kernel.root / "app" / "models.py").write_text("# changed\n", encoding="utf-8")
    rb = kernel.rollback_snapshot(snap["id"])
    assert rb["ok"] is False and rb["blocked"] == ["app/models.py"]
    assert (kernel.root / "app" / "models.py").read_text(encoding="utf-8") == "# changed\n"
    assert kernel.check_path_policy("app/models.py") == {
        "path": "app/models.py", "read": True, "write": False, "matched_deny": None, "matched_read_only": "app/models.py",
        "scan_ignored": False, "policy": str(kernel.aether_dir / "policy.json")}
    assert "POLICY_BLOCKED" in {e["type"] for e in kernel.db.recent_events(40)}
    with TestClient(create_app(kernel, token="t")) as c:
        assert c.get("/api/file", params={"path": "app/payments.py"}).status_code == 403
        assert "backend:app/payments.py" not in {n["key"] for n in c.get("/api/graph").json()["nodes"]}
        assert c.get("/api/policy").json()["deny"] == ["app/payments.py"]


def test_policy_reloads_on_change_and_fails_closed_on_invalid_json(kernel: AetherKernel) -> None:
    assert kernel.policy.source == "default"
    path = kernel.aether_dir / "policy.json"
    path.write_text(json.dumps({"read_only": ["app/**"]}), encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert kernel.policy.is_read_only("app/api.py") and kernel.policy.is_denied("id_rsa")  # default deny kept
    path.write_text("{ not json", encoding="utf-8")
    os.utime(path, (time.time() + 10, time.time() + 10))
    pol = kernel.policy
    assert not pol.can_write("README.md") and not pol.command_allowed(["pytest"]) and "lockdown" in pol.source


async def test_check_path_policy_over_mcp(kernel: AetherKernel) -> None:
    kernel.save_policy(Policy(read_only=["app/**"]))
    async with Client(build_server(kernel)) as client:
        res = await client.call_tool("check_path_policy", {"path": "app/api.py"})
        pol = await client.read_resource("aether://policy")
    assert res.data["write"] is False and res.data["read"] is True
    assert json.loads(pol[0].text)["read_only"] == ["app/**"]


# ---------------------------------------------------------------------------- budget


def test_budget_tracker_emits_warnings_on_threshold_crossings(kernel: AetherKernel) -> None:
    seen: list[dict] = []
    kernel.bus.add_listener(lambda ev: seen.append(ev.payload) if ev.type == "BUDGET_WARNING" else None)
    tracker = BudgetTracker(kernel.db, kernel.bus, threshold=1000, session="s-test")
    tracker.record("query_smart_context", "x", tokens=500, files=["app/api.py"], query="users")
    assert not seen
    st = tracker.record("impact_analysis", "x", tokens=350, files=["app/api.py", "app/models.py"])
    assert st["warning"] and seen[-1]["level"] == "approaching" and st["pct"] == 85.0
    tracker.record("check_scars", "x", tokens=100)
    assert len(seen) == 1  # no new level crossed
    tracker.record("query_smart_context", "x", tokens=200, query="payments")
    assert len(seen) == 2 and seen[-1]["level"] == "exceeded" and seen[-1]["used"] == 1150
    report = tracker.report("s-test")
    assert report["used"] == 1150 and report["calls"] == 4 and report["by_tool"][0]["tool"] == "query_smart_context"
    assert report["top_files"][0] == {"file": "app/api.py", "tokens": 675}
    assert report["top_queries"][0] == {"query": "users", "tokens": 500}
    assert files_in({"results": [{"file": "a.py"}, {"file_path": "b.py"}], "x": {"path": "c.py"}}) == ["a.py", "b.py", "c.py"]


async def test_mcp_tool_responses_are_metered(kernel: AetherKernel) -> None:
    kernel.set_budget_threshold(1000)
    kernel.budget.threshold = 1000
    async with Client(build_server(kernel)) as client:
        await client.call_tool("query_smart_context", {"query": "charge customer payment", "max_tokens": 1500})
        await client.call_tool("impact_analysis", {"file_path": "app/api.py"})
        await client.call_tool("check_scars", {"file_path": "app/api.py"})
        budget = (await client.call_tool("get_context_budget", {})).data
    tools = {t["tool"]: t for t in budget["by_tool"]}
    assert {"query_smart_context", "impact_analysis", "check_scars"} <= set(tools)
    assert budget["session"] == kernel.budget.session and budget["used"] > 0 and budget["threshold"] == 1000
    assert budget["top_queries"][0]["query"] in {"charge customer payment", "app/api.py"}
    stats = kernel.get_telemetry_stats()
    # the get_context_budget response itself is metered too, so the running total keeps growing
    assert stats["budget"]["session"] == kernel.budget.session and stats["budget"]["used"] > budget["used"]
    assert json.loads((kernel.aether_dir / "config.json").read_text(encoding="utf-8"))["budget_tokens"] == 1000
    if budget["used"] >= 800:
        assert "BUDGET_WARNING" in {e["type"] for e in kernel.db.recent_events(60)}


def test_budget_warning_reaches_the_websocket(kernel: AetherKernel) -> None:
    with TestClient(create_app(kernel, token="t")) as c, c.websocket_connect("/ws/telemetry") as ws:
        assert ws.receive_json()["type"] == "HELLO"
        BudgetTracker(kernel.db, kernel.bus, threshold=1000, session="ws").record("query_smart_context", "x", tokens=1200)
        for _ in range(10):
            msg = ws.receive_json()
            if msg["type"] == "EVENT" and msg["event"]["type"] == "BUDGET_WARNING":
                assert msg["event"]["payload"]["level"] == "exceeded"
                break
        else:
            raise AssertionError("BUDGET_WARNING never reached the websocket")
        assert c.get("/api/budget", params={"session": "ws"}).json()["used"] == 1200


# ---------------------------------------------------------------------------- share card


def test_share_card_is_self_contained_svg_with_real_metrics(kernel: AetherKernel, tmp_path) -> None:
    for q in ("charge customer payment", "user repository get user", "format money"):
        kernel.query_smart_context(q)
    out = tmp_path / "card.svg"
    r = kernel.share_card(out)
    svg = out.read_text(encoding="utf-8")
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") and root.attrib["width"] == "1200" and root.attrib["height"] == "630"
    m = r["metrics"]
    assert m["queries"] == 3 and m["files_indexed"] == kernel.db.counts()["files"] and m["p50_ms"] > 0
    assert human_int(m["tokens_saved"]) in svg and f"{m['savings_pct']:.1f}%" in svg and "backend" in svg
    assert "#05070d" in svg and "#00f0ff" in svg and "#8b5cf6" in svg
    lowered = svg.lower()
    for forbidden in ("<image", "href=", "@import", "url(http", "<script", "fonts.googleapis"):
        assert forbidden not in lowered, forbidden
    assert human_usd(0.5) == "$0.5000" and human_usd(12.3) == "$12.30" and human_int(1_234_567) == "1.2M"
    empty = render_share_card({"project": "<x&y>"})
    assert "&lt;x&amp;y&gt;" in empty and ET.fromstring(empty) is not None
