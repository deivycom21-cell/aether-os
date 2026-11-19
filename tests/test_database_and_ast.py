"""Storage layer, multi-language parsers, indexer, resolver and hybrid search."""

from __future__ import annotations

from pathlib import Path

from aether_core.ast_indexer import (
    IgnoreRules,
    Indexer,
    find_block_end,
    iter_source_files,
    normalize_route,
    parse_dockerfile,
    parse_go,
    parse_js_ts,
    parse_json,
    parse_markdown,
    parse_python,
    parse_rust,
    parse_sql,
)
from aether_core.context_engine import HybridSearcher, estimate_tokens, savings_breakdown
from aether_core.database import AetherDB, split_identifier
from aether_core.kernel import AetherKernel


def kinds(parsed, kind):
    return {n["name"]: n for n in parsed.nodes if n["kind"] == kind}


# ---------------------------------------------------------------------------- database


def test_database_uses_wal_and_fts(tmp_path: Path) -> None:
    db = AetherDB(tmp_path / "b.db")
    assert db.scalar("PRAGMA journal_mode").lower() == "wal"
    assert db.fts_enabled
    assert db.get_meta("schema_version") == "1"
    db.close()


def test_split_identifier() -> None:
    assert split_identifier("getUserById") == ["get", "user", "by", "id"]
    assert split_identifier("HTTPServer_v2") == ["http", "server", "v", "2"]
    assert split_identifier("charge_customer") == ["charge", "customer"]


def test_replace_file_is_atomic_and_searchable(tmp_path: Path) -> None:
    db = AetherDB(tmp_path / "b.db")
    rid = db.upsert_repo("r", tmp_path, True)
    nodes = [{"kind": "function", "name": "computeInvoiceTotal", "qualname": "m.computeInvoiceTotal",
              "signature": "def computeInvoiceTotal(items)", "docstring": "Sum invoice lines.", "body": "return sum(items)",
              "start_line": 1, "end_line": 2}]
    fid = db.replace_file(rid, "m.py", "python", "sha", 10, 2, 5, nodes, [{"kind": "calls", "dst_name": "sum", "src_qualname": "m.computeInvoiceTotal"}])
    hits = db.fts_search("invoice total")
    assert hits and db.nodes_by_ids([hits[0][0]])[0].name == "computeInvoiceTotal"
    # Re-writing replaces nodes, edges and FTS rows instead of duplicating them.
    db.replace_file(rid, "m.py", "python", "sha2", 10, 2, 5, nodes, [])
    assert db.scalar("SELECT COUNT(*) FROM nodes WHERE file_id = ?", (fid,)) == 1
    assert db.scalar("SELECT COUNT(*) FROM edges WHERE src_file_id = ?", (fid,)) == 0
    assert db.scalar("SELECT COUNT(*) FROM nodes_fts") == 1
    db.delete_file(fid)
    assert db.counts()["nodes"] == 0 and db.scalar("SELECT COUNT(*) FROM nodes_fts") == 0
    db.close()


def test_scars_dedupe_severity_and_hits(tmp_path: Path) -> None:
    db = AetherDB(tmp_path / "b.db")
    sid, created = db.add_scar("app/pay.py", "Double charge on retry", "Idempotency key", "critical")
    sid2, created2 = db.add_scar("app\\pay.py", "double charge   on retry", "Idempotency key v2", "high")
    assert created and not created2 and sid == sid2
    db.add_scar("app/pay.py", "Rounding error", "Use Decimal", "low")
    scars = db.scars_for_path("./app/pay.py")
    assert [s.error_summary for s in scars][0] == "Double charge on retry"  # most severe first
    assert scars[0].fix_applied == "Idempotency key v2"
    assert db.scars_for_path("app/pay.py", record_hit=False)[0].hit_count == 1
    assert db.add_scar("x.py", "e", severity="bogus")[1] and db.list_scars()[0].severity == "high"
    db.close()


def test_telemetry_and_events(tmp_path: Path) -> None:
    db = AetherDB(tmp_path / "b.db")
    db.record_telemetry("context_query", 10_000, 1_000, 0.027, 12.5)
    db.record_telemetry("context_query", 5_000, 500, 0.0135, 7.5)
    t = db.telemetry_totals()
    assert t == {"queries": 2, "raw_tokens": 15000, "compressed_tokens": 1500, "saved_tokens": 13500,
                 "saved_usd": 0.0405, "avg_latency_ms": 10.0}
    first = db.insert_event("A", "o1", ["r:x"], {"k": 1}, 1.0)
    db.insert_event("B", "o2", [], {}, 2.0)
    assert [e["type"] for e in db.events_since(first)] == ["B"]
    assert db.recent_events(1)[0]["type"] == "B"
    db.close()


# ---------------------------------------------------------------------------- parsers


def test_parse_python_symbols_routes_and_imports() -> None:
    src = Path(__file__).parent / "conftest.py"
    from tests.conftest import BACKEND_FILES

    parsed = parse_python(BACKEND_FILES["app/api.py"], "app/api.py")
    routes = kinds(parsed, "route")
    assert set(routes) == {"GET /api/users/{user_id}", "POST /api/payments", "GET /health"}
    assert routes["GET /api/users/{user_id}"]["metadata"]["normalized"] == "/api/users/:param"
    assert routes["POST /api/payments"]["metadata"]["handler"] == "app.api.create_payment"
    funcs = kinds(parsed, "function")
    assert funcs["read_user"]["signature"] == "def read_user(user_id: int) -> dict"
    assert funcs["read_user"]["docstring"] == "Fetch one user by id."
    imports = {e["dst_name"] for e in parsed.edges if e["kind"] == "imports"}
    assert {"fastapi.APIRouter", "app.models.UserRepository", "app.payments.charge_customer"} <= imports
    calls = {e["dst_name"] for e in parsed.edges if e["kind"] == "calls"}
    assert "charge_customer" in calls
    models = parse_python(BACKEND_FILES["app/models.py"], "app/models.py")
    assert kinds(models, "method")["get_user"]["qualname"] == "app.models.UserRepository.get_user"
    rel = parse_python(BACKEND_FILES["app/payments.py"], "app/payments.py")
    assert any(e["dst_name"] == "app.models.User" for e in rel.edges if e["kind"] == "imports")
    assert src.exists()


def test_parse_python_syntax_error_degrades_gracefully() -> None:
    parsed = parse_python("def broken(:\n  pass", "bad.py")
    assert parsed.nodes[0]["kind"] == "module" and "parse_error" in parsed.nodes[0]["metadata"]


def test_parse_typescript() -> None:
    from tests.conftest import FRONTEND_FILES

    parsed = parse_js_ts(FRONTEND_FILES["src/api/client.ts"], "src/api/client.ts", "typescript")
    funcs = kinds(parsed, "function")
    assert "fetchUser" in funcs and "payInvoice" in funcs
    assert funcs["fetchUser"]["docstring"] == "Load one user from the backend."
    assert "interface" in {n["kind"] for n in parsed.nodes}
    calls = {n["name"] for n in parsed.nodes if n["kind"] == "api_call"}
    assert calls == {"GET /api/users/:param", "POST /api/payments"}
    assert any(e["dst_name"] == "../utils/format" for e in parsed.edges if e["kind"] == "imports")
    fmt = parse_js_ts(FRONTEND_FILES["src/utils/format.ts"], "src/utils/format.ts", "typescript")
    assert "format" in kinds(fmt, "method") and "Formatter" in kinds(fmt, "class")
    nxt = parse_js_ts(FRONTEND_FILES["app/api/hello/route.ts"], "app/api/hello/route.ts", "typescript")
    assert "GET /api/hello" in kinds(nxt, "route")


def test_parse_express_routes_and_axios() -> None:
    src = "const router = express.Router();\nrouter.get('/api/items/:id', handler);\napp.post(\"/login\", login);\naxios.delete(`${BASE}/api/items/${id}`);\n"
    parsed = parse_js_ts(src, "server.js", "javascript")
    assert {"GET /api/items/:id", "POST /login"} <= set(kinds(parsed, "route"))
    assert "DELETE /api/items/:param" in kinds(parsed, "api_call")


def test_parse_go_and_rust() -> None:
    go = '''package main

import (
    "fmt"
    "net/http"
)

// Server handles requests.
type Server struct { port int }

// Start boots the server.
func (s *Server) Start() error {
    http.HandleFunc("/api/ping", ping)
    return nil
}

func ping(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "pong") }
'''
    parsed = parse_go(go, "cmd/main.go")
    assert kinds(parsed, "method")["Start"]["docstring"] == "Start boots the server."
    assert "ping" in kinds(parsed, "function") and "Server" in kinds(parsed, "class")
    assert "ANY /api/ping" in kinds(parsed, "route")
    assert {"fmt", "net/http"} <= {e["dst_name"] for e in parsed.edges}
    rs = '''use crate::db::Pool;

/// A user record.
pub struct User { id: u64 }

impl User {
    /// Create a user.
    pub fn new(id: u64) -> Self { User { id } }
}

#[get("/users/{id}")]
async fn get_user() -> String { String::new() }
'''
    parsed = parse_rust(rs, "src/main.rs")
    assert kinds(parsed, "method")["new"]["docstring"] == "Create a user."
    assert "GET /users/{id}" in kinds(parsed, "route") and "User" in kinds(parsed, "class")


def test_parse_data_and_infra_files() -> None:
    from tests.conftest import BACKEND_FILES, FRONTEND_FILES

    pkg = parse_json(FRONTEND_FILES["package.json"], "package.json")
    assert {"react", "next"} == set(kinds(pkg, "dependency")) and "dev" in kinds(pkg, "script")
    docker = parse_dockerfile(BACKEND_FILES["Dockerfile"], "Dockerfile")
    assert kinds(docker, "docker_stage")["runtime"]["metadata"]["ports"] == ["8000"]
    md = parse_markdown(BACKEND_FILES["README.md"], "README.md")
    assert {"Backend", "Payments", "Users"} == set(kinds(md, "section"))
    sql = parse_sql("CREATE TABLE IF NOT EXISTS users (id int);\nCREATE INDEX idx ON users(id);", "s.sql")
    assert "users" in kinds(sql, "table") and "idx" in kinds(sql, "index")


def test_normalize_route_and_block_end() -> None:
    assert normalize_route("/users/{id}") == normalize_route("/users/:id") == normalize_route("/users/[id]") == "/users/:param"
    assert normalize_route("https://api.example.com/v1/items?x=1") == "/v1/items"
    text = 'function f() { const s = "}"; if (x) { return 1; } }\nnext'
    assert text[: find_block_end(text, 0)].endswith("}") and "next" not in text[: find_block_end(text, 0)]


def test_token_estimator_is_calibrated() -> None:
    text = "def charge_customer(user, amount_cents):\n    return {'status': 'charged'}\n" * 20
    est = estimate_tokens(text)
    assert 0.5 * len(text) / 4 < est < 1.6 * len(text) / 4
    s = savings_breakdown(10_000, 1_000)
    assert s["saved_tokens"] == 9000 and s["savings_pct"] == 90.0 and s["saved_usd"]["opus"] > s["saved_usd"]["sonnet"]


# ---------------------------------------------------------------------------- indexer + resolver


def test_ignore_rules_and_sensitive_files(backend_repo: Path) -> None:
    (backend_repo / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    (backend_repo / "id_rsa").write_text("x", encoding="utf-8")
    files = {p.relative_to(backend_repo).as_posix() for p in iter_source_files(backend_repo)}
    assert "app/api.py" in files and "Dockerfile" in files
    assert not any(f.startswith("node_modules") for f in files)
    assert "debug.log" not in files and ".env" not in files and "id_rsa" not in files
    assert IgnoreRules(backend_repo).ignored("build", True)


def test_incremental_indexing(tmp_path: Path, backend_repo: Path) -> None:
    db = AetherDB(tmp_path / "b.db")
    idx = Indexer(db)
    first = idx.index_repo(backend_repo, "backend", primary=True)
    assert first.indexed == first.scanned and first.indexed >= 7
    second = idx.index_repo(backend_repo, "backend")
    assert second.indexed == 0 and second.unchanged == first.scanned
    (backend_repo / "app" / "payments.py").write_text("def refund():\n    return 1\n", encoding="utf-8")
    (backend_repo / "app" / "models.py").unlink()
    third = idx.index_repo(backend_repo, "backend")
    assert third.indexed == 1 and third.removed == 1
    assert db.file_by_path("app/models.py") is None
    db.close()


def test_parallel_parsing_matches_sequential(tmp_path: Path, backend_repo: Path, monkeypatch) -> None:
    import aether_core.ast_indexer as ai

    seq_db, par_db = AetherDB(tmp_path / "seq.db"), AetherDB(tmp_path / "par.db")
    Indexer(seq_db).index_repo(backend_repo, "b")
    monkeypatch.setattr(ai, "PARALLEL_THRESHOLD", 1)
    stats = Indexer(par_db).index_repo(backend_repo, "b")
    assert stats.indexed >= 7 and not stats.errors
    q = "SELECT f.path, n.kind, n.qualname, n.signature FROM nodes n JOIN files f ON f.id = n.file_id ORDER BY 1, 2, 3"
    assert [tuple(r) for r in seq_db.query(q)] == [tuple(r) for r in par_db.query(q)]
    seq_db.close()
    par_db.close()


def test_cross_repo_resolution(kernel: AetherKernel) -> None:
    db = kernel.db
    api = db.file_by_path("app/api.py", "backend")
    models = db.file_by_path("app/models.py", "backend")
    assert api and models
    deps = {d["dst_path"] for d in db.file_dependencies(api.id) if d["kind"] == "imports"}
    assert {"app/models.py", "app/payments.py"} <= deps
    # Relative import in payments.py resolves to models.py
    pay = db.file_by_path("app/payments.py")
    assert "app/models.py" in {d["dst_path"] for d in db.file_dependencies(pay.id)}
    # Frontend fetch("/api/payments") links to the backend route in another repo.
    dependents = db.file_dependents(api.id)
    http = [d for d in dependents if d["kind"] == "http_call"]
    assert {d["repo_name"] for d in http} == {"frontend"} and {d["path"] for d in http} == {"src/api/client.ts"}
    # TS relative import resolves with extension inference.
    client = db.file_by_path("src/api/client.ts", "frontend")
    assert "src/utils/format.ts" in {d["dst_path"] for d in db.file_dependencies(client.id)}
    # Calls resolve through the import graph.
    call_edges = db.query("SELECT e.* FROM edges e WHERE e.src_file_id = ? AND e.kind = 'calls' AND e.dst_name = 'charge_customer'", (api.id,))
    assert call_edges and call_edges[0]["dst_file_id"] == pay.id


def test_hybrid_search_ranks_relevant_symbol(kernel: AetherKernel) -> None:
    results = HybridSearcher(kernel.db).search("charge a customer payment", limit=5)
    assert results[0][0].name == "charge_customer"
    vec = HybridSearcher(kernel.db)
    vec.ensure_vectors()
    assert vec.vectors.version == kernel.db.index_version() and vec.vectors.matrix.shape[0] == kernel.db.counts()["nodes"]
