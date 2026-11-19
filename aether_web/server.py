"""FastAPI server: REST inspection endpoints, ``/ws/telemetry`` live stream and the 3D dashboard.

Security model (localhost-only tool):
* binds to 127.0.0.1 by default; a Host-header allowlist blocks DNS-rebinding attacks;
* mutating endpoints (rollback, snapshot, scan, reindex) require the per-process ``X-Aether-Token``
  that is injected into the served page, so third-party sites cannot trigger them via CSRF;
* WebSocket connections from foreign origins are rejected.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from aether_core import __version__
from aether_core.event_bus import DBEventTailer
from aether_core.kernel import AetherKernel

STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1", "testserver"}


class QueryBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    max_tokens: int = Field(default=1500, ge=200, le=20000)


class SnapshotBody(BaseModel):
    label: str = Field(default="dashboard", max_length=200)


class ScanBody(BaseModel):
    target: str = Field(default=".", max_length=1000)


def _host_only(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.split(":")[0]


def create_app(kernel: AetherKernel | None = None, root: Path | str | None = None, token: str | None = None,
               extra_hosts: set[str] | None = None) -> FastAPI:
    k = kernel or AetherKernel(root)
    api_token = token or secrets.token_urlsafe(24)
    hosts = ALLOWED_HOSTS | {h.lower() for h in (extra_hosts or set())}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tailer = DBEventTailer(k.db, k.bus)
        tailer.start()
        app.state.tailer = tailer
        try:
            yield
        finally:
            await tailer.stop()

    app = FastAPI(title="AetherOS", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.kernel = k
    app.state.token = api_token

    @app.middleware("http")
    async def host_guard(request: Request, call_next: Any) -> Any:
        host = _host_only(request.headers.get("host", ""))
        if host not in hosts:
            return JSONResponse({"detail": "Host not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def require_token(x_aether_token: str = Header(default="")) -> None:
        if not secrets.compare_digest(x_aether_token, api_token):
            raise HTTPException(status_code=403, detail="Missing or invalid X-Aether-Token")

    # ------------------------------------------------------------------ page
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__AETHER_TOKEN__", api_token).replace("__AETHER_VERSION__", __version__))

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "project": k.repo_name}

    # ------------------------------------------------------------------ read-only API
    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        return await asyncio.to_thread(k.get_telemetry_stats)

    @app.get("/api/graph")
    async def graph(limit: int = Query(default=800, ge=1, le=5000)) -> dict[str, Any]:
        g = await asyncio.to_thread(k.db.file_graph, limit)
        spots = await asyncio.to_thread(k.hotspot_map)
        risk = {(h.repo, h.file): h.risk_score for h in spots}
        pol = k.policy
        nodes = [
            {
                "id": int(n["id"]), "key": f"{n['repo_name']}:{n['path']}", "path": n["path"], "repo": n["repo_name"],
                "language": n["language"], "lines": int(n["line_count"]), "tokens": int(n["token_estimate"]),
                "symbols": int(n["node_count"]), "scars": int(n["scar_count"]), "routes": int(n["route_count"]),
                "risk": float(risk.get((n["repo_name"], n["path"]), 0.0)),
            }
            for n in g["nodes"] if pol.can_read(n["path"])
        ]
        edges = [{"source": int(e["src"]), "target": int(e["dst"]), "kind": e["kind"], "weight": int(e["weight"])} for e in g["edges"]]
        return {"nodes": nodes, "edges": edges, "repos": [r["name"] for r in k.db.list_repos()]}

    @app.get("/api/file")
    async def file_detail(path: str, repo: str | None = None) -> dict[str, Any]:
        f = k.db.file_by_path(path, repo)
        if f is None:
            raise HTTPException(404, "file not indexed")
        if not k.policy.can_read(f.path):
            raise HTTPException(403, "denied by .aether/policy.json")
        nodes = k.db.nodes_for_file(f.id)
        symbols = [{"kind": n.kind, "name": n.name, "signature": n.signature, "line": n.start_line}
                   for n in nodes if n.kind != "module"][:60]
        masked, _ = k.vault.mask_obj(symbols)
        return {
            "repo": f.repo_name, "path": f.path, "language": f.language, "lines": f.line_count, "tokens": f.token_estimate,
            "symbols": masked,
            "depends_on": sorted({d["dst_path"] for d in k.db.file_dependencies(f.id) if d["dst_path"]})[:30],
            "used_by": sorted({d["path"] for d in k.db.file_dependents(f.id)})[:30],
            "scars": [s.to_dict() for s in k.db.scars_for_path(f.path, record_hit=False)],
        }

    @app.get("/api/events")
    async def events(since: int = 0, limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return k.db.events_since(since, limit) if since else k.db.recent_events(limit)

    @app.get("/api/scars")
    async def scars() -> list[dict[str, Any]]:
        masked, _ = k.vault.mask_obj([s.to_dict() for s in k.db.list_scars(500)])
        return masked

    @app.get("/api/snapshots")
    async def snapshots() -> list[dict[str, Any]]:
        return k.snapshots.list()

    @app.get("/api/repos")
    async def repos() -> list[dict[str, Any]]:
        return k.db.list_repos()

    @app.get("/api/impact")
    async def impact(file: str) -> dict[str, Any]:
        return await asyncio.to_thread(k.impact_analysis, file)

    @app.get("/api/swarm")
    async def swarm() -> list[dict[str, Any]]:
        return k.db.list_swarm_runs(20)

    @app.get("/api/hotspots")
    async def hotspots(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, Any]:
        return await asyncio.to_thread(k.get_code_hotspots, limit)

    @app.get("/api/budget")
    async def budget(session: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(k.budget_report, session)

    @app.get("/api/policy")
    async def policy() -> dict[str, Any]:
        return {**k.policy.to_dict(), "source": k.policy.source}

    @app.get("/api/checkpoints")
    async def checkpoints() -> list[dict[str, Any]]:
        masked, _ = k.vault.mask_obj(k.db.list_checkpoints(20))
        return masked

    # ------------------------------------------------------------------ mutating API (token-protected)
    @app.post("/api/query", dependencies=[Depends(require_token)])
    async def query(body: QueryBody) -> dict[str, Any]:
        return await asyncio.to_thread(k.query_smart_context, body.query, body.max_tokens)

    @app.post("/api/snapshots", dependencies=[Depends(require_token)])
    async def create_snapshot(body: SnapshotBody) -> dict[str, Any]:
        return await asyncio.to_thread(k.create_snapshot, body.label)

    @app.post("/api/snapshots/{snapshot_id}/rollback", dependencies=[Depends(require_token)])
    async def rollback(snapshot_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(k.rollback_snapshot, snapshot_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/scan", dependencies=[Depends(require_token)])
    async def scan(body: ScanBody) -> dict[str, Any]:
        return await asyncio.to_thread(k.scan_security, body.target)

    @app.post("/api/index", dependencies=[Depends(require_token)])
    async def reindex() -> dict[str, Any]:
        return await asyncio.to_thread(k.index)

    # ------------------------------------------------------------------ websocket
    @app.websocket("/ws/telemetry")
    async def telemetry(ws: WebSocket) -> None:
        origin = ws.headers.get("origin")
        if origin:
            o_host = _host_only(origin.split("://", 1)[-1])
            if o_host not in hosts:
                await ws.close(code=1008)
                return
        await ws.accept()
        sub = k.bus.subscribe()
        try:
            stats_now = await asyncio.to_thread(k.get_telemetry_stats)
            await ws.send_json({"type": "HELLO", "stats": stats_now, "events": k.db.recent_events(40)})

            async def pump_events() -> None:
                while True:
                    ev = await sub.queue.get()
                    await ws.send_json({"type": "EVENT", "event": ev.to_dict()})

            async def pump_stats() -> None:
                while True:
                    await asyncio.sleep(2.5)
                    s = await asyncio.to_thread(k.get_telemetry_stats)
                    await ws.send_json({"type": "STATS", "stats": s})

            async def drain_client() -> None:
                while True:
                    msg = await ws.receive_text()
                    if msg == "ping":
                        await ws.send_json({"type": "PONG"})

            tasks = [asyncio.create_task(c()) for c in (pump_events, pump_stats, drain_client)]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                t.exception()  # retrieve so asyncio doesn't log "exception never retrieved" on disconnect
        except WebSocketDisconnect:
            pass
        finally:
            k.bus.unsubscribe(sub)

    return app


def run_server(root: Path | str | None = None, host: str = "127.0.0.1", port: int = 7777, open_browser: bool = False) -> None:
    import threading
    import webbrowser

    import uvicorn

    kernel = AetherKernel(root)
    if kernel.db.counts()["files"] == 0:
        kernel.index()
    extra = {host} if host not in {"127.0.0.1", "localhost", "::1"} else set()
    app = create_app(kernel, extra_hosts=extra)
    url = f"http://{'127.0.0.1' if host in {'0.0.0.0', '::'} else host}:{port}/"
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
