"""Web dashboard API/WebSocket, cross-process event tailing, plugins and the TUI."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aether_core.event_bus import PROCESS_ORIGIN, DBEventTailer, EventBus
from aether_core.kernel import AetherKernel
from aether_web.server import create_app
from plugins.base import PluginManager, audit_plugin_source
from plugins.docker_inspector import parse_compose
from plugins.env_shield import parse_env
from tests.conftest import fake_secret, git, git_init, requires_git

TOKEN = "test-token-123"


@pytest.fixture()
def client(kernel: AetherKernel) -> TestClient:
    with TestClient(create_app(kernel, token=TOKEN)) as c:
        yield c


# ---------------------------------------------------------------------------- web API


def test_dashboard_page_and_read_only_api(client: TestClient, kernel: AetherKernel) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert TOKEN in page.text and "__AETHER_TOKEN__" not in page.text
    assert "three@0.160.0" in page.text and "createCanvasViz" in page.text and "/ws/telemetry" in page.text
    stats = client.get("/api/stats").json()
    assert stats["files_indexed"] == kernel.db.counts()["files"]
    graph = client.get("/api/graph").json()
    keys = {n["key"] for n in graph["nodes"]}
    assert {"backend:app/api.py", "frontend:src/api/client.ts"} <= keys and set(graph["repos"]) == {"backend", "frontend"}
    by_id = {n["id"]: n["key"] for n in graph["nodes"]}
    links = {(by_id[e["source"]], by_id[e["target"]], e["kind"]) for e in graph["edges"]}
    assert ("frontend:src/api/client.ts", "backend:app/api.py", "http_call") in links
    detail = client.get("/api/file", params={"path": "app/models.py"}).json()
    assert {"UserRepository", "get_user"} <= {s["name"] for s in detail["symbols"]} and "app/api.py" in detail["used_by"]
    assert client.get("/api/file", params={"path": "missing.py"}).status_code == 404
    assert client.get("/api/impact", params={"file": "app/payments.py"}).json()["found"]
    assert client.get("/healthz").json()["ok"]


def test_mutating_endpoints_require_token(client: TestClient, kernel: AetherKernel) -> None:
    assert client.post("/api/query", json={"query": "user"}).status_code == 403
    assert client.post("/api/snapshots", json={"label": "x"}, headers={"X-Aether-Token": "wrong"}).status_code == 403
    h = {"X-Aether-Token": TOKEN}
    q = client.post("/api/query", json={"query": "charge customer"}, headers=h).json()
    assert q["results"] and "metrics" in q
    snap = client.post("/api/snapshots", json={"label": "web"}, headers=h).json()
    target = kernel.root / "app" / "models.py"
    original = target.read_text(encoding="utf-8")
    target.write_text("# broken\n", encoding="utf-8")
    rb = client.post(f"/api/snapshots/{snap['id']}/rollback", headers=h).json()
    assert rb["restored"] == 1 and target.read_text(encoding="utf-8") == original
    assert client.post("/api/snapshots/nope-404/rollback", headers=h).status_code == 404
    assert client.post("/api/snapshots/..%5Cx/rollback", headers=h).status_code in {400, 404}
    assert client.get("/api/snapshots").json()[0]["label"].startswith("auto: before rollback")
    assert client.post("/api/scan", json={"target": "app"}, headers=h).json()["risk_level"] == "clean"
    assert client.post("/api/query", json={"query": ""}, headers=h).status_code == 422


def test_host_header_guard_blocks_dns_rebinding(client: TestClient) -> None:
    assert client.get("/api/stats", headers={"host": "evil.example.com"}).status_code == 403
    assert client.get("/api/stats", headers={"host": "127.0.0.1:7777"}).status_code == 200


def test_websocket_streams_hello_and_live_events(client: TestClient, kernel: AetherKernel) -> None:
    with client.websocket_connect("/ws/telemetry") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "HELLO" and hello["stats"]["project"] == "backend"
        kernel.query_smart_context("user repository")
        for _ in range(10):
            msg = ws.receive_json()
            if msg["type"] == "EVENT" and msg["event"]["type"] == "CONTEXT_QUERY":
                break
        else:
            pytest.fail("CONTEXT_QUERY event not received")
        assert any(n.startswith("backend:") for n in msg["event"]["nodes"])
        ws.send_text("ping")


def test_websocket_rejects_foreign_origin(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/telemetry", headers={"origin": "https://evil.example.com"}) as ws:
            ws.receive_json()


async def test_db_tailer_forwards_events_from_other_processes(tmp_path: Path) -> None:
    from aether_core.database import AetherDB

    db = AetherDB(tmp_path / "b.db")
    bus = EventBus(db)
    tailer = DBEventTailer(db, bus)
    sub = bus.subscribe()
    db.insert_event("SWARM_STEP", "other-host:1234:abc", ["swarm:x:builder"], {"step": "committed"}, 1.0)
    bus.publish("CONTEXT_QUERY", {"query": "local"})  # same-process event: delivered directly, not re-delivered
    delivered = tailer.poll_once()
    assert [e.type for e in delivered] == ["SWARM_STEP"]
    got = [await asyncio.wait_for(sub.queue.get(), 1), await asyncio.wait_for(sub.queue.get(), 1)]
    assert {e.type for e in got} == {"CONTEXT_QUERY", "SWARM_STEP"}
    assert all(e.origin != PROCESS_ORIGIN for e in got if e.type == "SWARM_STEP")
    bus.unsubscribe(sub)
    db.close()


# ---------------------------------------------------------------------------- plugins


COMPOSE = """
services:
  api:
    build: ./backend
    ports: ["8000:8000"]
    environment:
      DATABASE_URL: postgres://u:p@db/app
      LOG_LEVEL: info
    depends_on: [db]
  db:
    image: postgres:16
    ports:
      - target: 5432
        published: 5433
"""


def test_docker_inspector(kernel: AetherKernel) -> None:
    services = parse_compose(COMPOSE)
    assert services["api"]["ports"] == ["8000:8000"] and services["api"]["depends_on"] == ["db"]
    assert services["api"]["env_keys"] == ["DATABASE_URL", "LOG_LEVEL"] and services["db"]["ports"] == ["5433:5432"]
    (kernel.root / "docker-compose.yml").write_text(COMPOSE.replace("./backend", "."), encoding="utf-8")
    result = PluginManager(kernel).run_named("docker_inspector")
    assert result.ok, result.error
    assert {s["service"] for s in result.summary["services"]} == {"api", "db"}
    svc = {n.name: n for n in kernel.db.nodes_by_kind("service")}
    assert "postgres" not in str(svc["api"].metadata.get("env_keys"))  # only names, never values
    compose = kernel.db.file_by_path("docker-compose.yml")
    builds = [d for d in kernel.db.file_dependencies(compose.id) if d["kind"] == "builds"]
    assert builds and builds[0]["dst_path"] == "Dockerfile"


def test_env_shield_reports_without_values(kernel: AetherKernel) -> None:
    secret_value = fake_secret("sk-proj-", 40)
    root = kernel.root
    (root / ".env").write_text(f"OPENAI_API_KEY={secret_value}\nDEBUG=true\nNEW_FLAG=1\n", encoding="utf-8")
    (root / ".env.example").write_text(f"OPENAI_API_KEY={fake_secret('sk-proj-', 40, seed=99)}\nDEBUG=false\nREDIS_URL=\n", encoding="utf-8")
    assert parse_env('export A="x y"\n# c\nB=2 # note\n') == {"A": "x y", "B": "2"}
    result = PluginManager(kernel).run_named("env_shield")
    assert result.ok, result.error
    rep = result.summary["reports"][0]
    assert rep["undocumented_keys"] == ["NEW_FLAG"] and rep["unset_keys"] == ["REDIS_URL"]
    assert rep["example_leaks"][0]["key"] == "OPENAI_API_KEY" and rep["status"] == "critical"
    assert "OPENAI_API_KEY" in rep["sensitive_keys"]
    assert secret_value not in str(result.to_dict())
    env_nodes = {n.name: n for n in kernel.db.nodes_by_kind("env_var")}
    assert set(env_nodes) == {"OPENAI_API_KEY", "DEBUG", "NEW_FLAG", "REDIS_URL"}
    assert all(secret_value not in n.signature + n.body for n in env_nodes.values())
    kernel.index(run_plugins=False)  # plugin-managed records survive re-indexing
    assert kernel.db.file_by_path(".env.example") is not None


@requires_git
def test_git_history_scars(backend_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_OLLAMA_URL", "http://127.0.0.1:9")
    git_init(backend_repo)
    pay = backend_repo / "app" / "payments.py"
    pay.write_text(pay.read_text(encoding="utf-8") + "\n# guard against double charge\n", encoding="utf-8")
    git(backend_repo, "commit", "-qam", "fix: crash on double charge when gateway retries")
    (backend_repo / "README.md").write_text("# Backend v2\n", encoding="utf-8")
    git(backend_repo, "commit", "-qam", "docs: update readme")
    k = AetherKernel(backend_repo)
    summary = k.index()
    plugin = next(p for p in summary["plugins"] if p["name"] == "git_history_scars")
    assert plugin["ok"] and plugin["summary"]["scars_created"] == 1
    scars = k.check_scars("app/payments.py")
    assert scars[0]["source"] == "git_history" and scars[0]["severity"] == "high"
    assert "double charge" in scars[0]["error_summary"]
    assert k.index()["plugins"][-1]["summary"]["scars_created"] == 0  # idempotent
    k.close()


def test_plugin_audit_and_trusted_local_plugins(kernel: AetherKernel) -> None:
    assert audit_plugin_source("import subprocess\nsubprocess.run(['x'])")
    assert audit_plugin_source("eval('1')") and audit_plugin_source("import os\nos.system('x')")
    assert audit_plugin_source("from pathlib import Path\nPath('.').exists()") == []
    plugin_dir = kernel.aether_dir / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "todo_counter.py").write_text(
        "from plugins.base import BaseAetherPlugin\n\n"
        "class TodoCounter(BaseAetherPlugin):\n"
        "    name = 'todo_counter'\n    description = 'Counts TODO markers.'\n\n"
        "    def run(self):\n"
        "        n = sum(p.read_text(encoding='utf-8').count('TODO') for p in (self.kernel.root / 'app').rglob('*.py'))\n"
        "        return {'todos': n}\n", encoding="utf-8")
    (plugin_dir / "evil.py").write_text("import socket\n", encoding="utf-8")
    pm = PluginManager(kernel)
    assert "todo_counter" not in {p["name"] for p in pm.describe()}  # not trusted yet
    kernel.config.trusted_plugins = ["todo_counter", "evil"]
    pm = PluginManager(kernel)
    assert "todo_counter" in {p["name"] for p in pm.describe()}
    assert any("evil.py: rejected by audit" in e for e in pm.load_errors)
    assert pm.run_named("todo_counter").summary == {"todos": 0}
    kernel.config.disabled_plugins = ["todo_counter"]
    assert "todo_counter" not in {p["name"] for p in PluginManager(kernel).describe()}


# ---------------------------------------------------------------------------- TUI


async def test_tui_renders_live_metrics(kernel: AetherKernel) -> None:
    from aether_tui.app import AetherTUI, Kpi

    kernel.query_smart_context("charge customer")
    kernel.create_snapshot("tui-test")
    kernel.db.add_scar("app/payments.py", "Double charge", "Idempotency", "critical")
    app = AetherTUI(kernel=kernel, poll_interval=0.1)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause(0.3)
        assert app.query_one("#k-scars", Kpi).value == "1"
        assert app.query_one("#k-graph", Kpi).value.startswith(str(kernel.db.counts()["files"]))
        assert app.query_one("#snaps").row_count == 1 and app.query_one("#scars").row_count == 1
        kernel.check_scars("app/payments.py")
        await pilot.pause(0.4)
        assert app.events_seen >= 2
        await pilot.press("c")
        await pilot.press("q")
