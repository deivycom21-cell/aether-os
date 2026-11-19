"""v0.2 · #6 hotspots, #9 semantic diff, #11 dead code, #14 living docs (Mermaid + endpoint table)."""

from __future__ import annotations

from fastmcp import Client

from aether_core.dead_code import is_test_path
from aether_core.hotspots import cyclomatic
from aether_core.kernel import AetherKernel
from aether_core.mcp_server import build_server
from aether_core.policy import Policy
from tests.conftest import git

COMPLEX_PAYMENTS = '''"""Payment processing with retry logic."""
from .models import User


def charge_customer(user: User, amount_cents: int, currency: str = "usd") -> dict:
    """Charge a customer; retries idempotently on network failure."""
    if amount_cents <= 0:
        raise ValueError("amount must be positive")
    for attempt in range(3):
        if attempt and currency == "eur" or currency == "gbp":
            continue
        while amount_cents > 10_000 and attempt < 2:
            amount_cents -= 1
            if amount_cents % 7 == 0:
                break
    return {"user": user.id, "amount": amount_cents, "currency": currency, "status": "charged"}
'''


def _commit_all(root, message: str) -> None:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


# ---------------------------------------------------------------------------- hotspots


def test_cyclomatic_complexity_estimate() -> None:
    assert cyclomatic("def f():\n    return 1\n") == 1
    assert cyclomatic("def f(x):\n    # if this or that\n    if x and y:\n        pass\n    for i in z:\n        pass\n") == 4
    assert cyclomatic("const f = (a) => a ? b : c && d;") == 3
    assert cyclomatic('s = "if or and while"') == 1


def test_hotspots_combine_complexity_fan_in_churn_and_scars(git_kernel: AetherKernel) -> None:
    root = git_kernel.root
    for i in range(4):
        text = COMPLEX_PAYMENTS.replace('"usd"', f'"usd{i}"')
        (root / "app" / "payments.py").write_text(text, encoding="utf-8")
        _commit_all(root, f"tune payments {i}")
    git_kernel.index(run_plugins=False)
    git_kernel.record_scar("app/payments.py", "Double charge", "Idempotency key", "critical")
    r = git_kernel.get_code_hotspots(limit=5)
    top = r["hotspots"][0]
    assert top["file"] == "app/payments.py" and top["level"] in {"high", "critical"}
    assert top["churn"] == 5 and top["scars"] == 1 and top["max_function_complexity"] >= 8 and top["fan_in"] >= 1
    assert 0 <= top["risk_score"] <= 100 and set(top["factors"]) == {"complexity", "fan_in", "churn", "scars"}
    assert any("scar" in reason for reason in top["reasons"])
    assert all(h["file"].endswith(".py") for h in r["hotspots"])  # README/Dockerfile are not code hotspots
    assert r["hotspots"] == sorted(r["hotspots"], key=lambda h: -h["risk_score"])
    # cache is invalidated by new scars
    git_kernel.record_scar("app/models.py", "Lost update", "", "high")
    assert {h["file"]: h["scars"] for h in git_kernel.get_code_hotspots(20)["hotspots"]}["app/models.py"] == 1


async def test_hotspots_exposed_over_mcp(kernel: AetherKernel) -> None:
    async with Client(build_server(kernel)) as client:
        res = await client.call_tool("get_code_hotspots", {"limit": 3})
    assert len(res.data["hotspots"]) == 3 and res.data["files_analyzed"] >= 5


# ---------------------------------------------------------------------------- dead code


def test_dead_code_finds_orphans_and_respects_entrypoints(kernel: AetherKernel) -> None:
    (kernel.root / "app" / "legacy.py").write_text('''"""Old helpers."""


def orphan_report(rows: list) -> int:
    return len(rows)


def _unused_private() -> None:
    return None


def used_helper() -> int:
    return 1


def caller() -> int:
    return used_helper()


registry = {"cb": caller}
''', encoding="utf-8")
    (kernel.root / "pyproject.toml").write_text('[project.scripts]\nsvc = "app.cli:main_entry"\n', encoding="utf-8")
    (kernel.root / "app" / "cli.py").write_text("def main_entry() -> None:\n    print('hi')\n", encoding="utf-8")
    kernel.index(run_plugins=False)
    r = kernel.find_dead_code("medium")
    names = {d["name"]: d for d in r["dead_symbols"]}
    assert {"orphan_report", "_unused_private"} <= set(names)
    assert names["_unused_private"]["confidence"] == "high" and names["orphan_report"]["confidence"] == "medium"
    for alive in ("used_helper", "caller", "main_entry", "read_user", "create_payment", "health", "charge_customer",
                  "test_positive_amount", "formatMoney", "GET"):
        assert alive not in names, alive
    # exported client functions nobody imports are reported, but only with medium confidence (public API)
    assert names["fetchUser"]["confidence"] == "medium" and names["fetchUser"]["repo"] == "frontend"
    assert r["excluded"]["registered"] >= 3 and r["excluded"]["test"] >= 1
    routes = {(u["method"], u["path"]) for u in r["unconsumed_routes"]}
    assert ("GET", "/health") in routes and ("POST", "/api/payments") not in routes
    high_only = kernel.find_dead_code("high")
    assert {d["name"] for d in high_only["dead_symbols"]} == {"_unused_private"}
    assert is_test_path("src/__tests__/a.test.ts") and not is_test_path("src/contest.py")


async def test_dead_code_over_mcp(kernel: AetherKernel) -> None:
    (kernel.root / "app" / "zombie.py").write_text("def zombie_function() -> int:\n    return 42\n", encoding="utf-8")
    kernel.index(run_plugins=False)
    async with Client(build_server(kernel)) as client:
        res = await client.call_tool("find_dead_code", {"min_confidence": "medium"})
    assert "zombie_function" in {d["name"] for d in res.data["dead_symbols"]}


# ---------------------------------------------------------------------------- semantic diff


def test_semantic_diff_reports_symbol_level_changes(git_kernel: AetherKernel) -> None:
    root = git_kernel.root
    payments = root / "app" / "payments.py"
    payments.write_text(payments.read_text(encoding="utf-8").replace(
        "def charge_customer(user: User, amount_cents: int, currency: str = \"usd\") -> dict:",
        "def charge_customer(user: User, amount_cents: int, currency: str = \"usd\", *, idempotency_key: str = \"\") -> dict:")
        + "\n\ndef refund_customer(user: User, amount_cents: int) -> dict:\n    return {\"refunded\": amount_cents}\n",
        encoding="utf-8")
    models = root / "app" / "models.py"
    models.write_text(models.read_text(encoding="utf-8")
                      .replace("    def save(self, user: User) -> None:\n        self._users[user.id] = user\n", "")
                      .replace("def get_user(self, user_id: int) -> User | None:\n        \"\"\"Return the user with ``user_id`` or None.\"\"\"\n        return self._users.get(user_id)",
                               "def find_user(self, user_id: int) -> User | None:\n        \"\"\"Return the user with ``user_id`` or None.\"\"\"\n        return self._users.get(user_id)"),
                      encoding="utf-8")
    api = root / "app" / "api.py"
    api.write_text(api.read_text(encoding="utf-8").replace('return {"ok": True}', 'return {"ok": True, "v": 2}')
                   + '\n\n@app.get("/version")\ndef version() -> dict:\n    return {"v": 2}\n', encoding="utf-8")
    (root / "app" / "new_module.py").write_text("class Ledger:\n    def post(self) -> None:\n        return None\n", encoding="utf-8")
    r = git_kernel.semantic_diff("HEAD")
    assert r["ok"]
    files = {f["path"]: f for f in r["files"]}
    pay = files["app/payments.py"]
    assert [s["symbol"] for s in pay["signature_changed"]] == ["app.payments.charge_customer"]
    assert "idempotency_key" in pay["signature_changed"][0]["after"]
    assert [a["symbol"] for a in pay["added"]] == ["app.payments.refund_customer"]
    mod = files["app/models.py"]
    assert [x["symbol"] for x in mod["removed"]] == ["app.models.UserRepository.save"]
    assert mod["renamed"] == [{"kind": "method", "from": "app.models.UserRepository.get_user",
                               "to": "app.models.UserRepository.find_user", "line": mod["renamed"][0]["line"]}]
    apif = files["app/api.py"]
    assert {a["kind"] for a in apif["added"]} == {"function", "route"}
    assert [m["symbol"] for m in apif["modified"]] == ["app.api.health"]
    assert files["app/new_module.py"]["status"] == "added"
    breaking = {(b["change"], b["symbol"]) for b in r["breaking"]}
    assert ("signature", "app.payments.charge_customer") in breaking and ("removed", "app.models.UserRepository.save") in breaking
    assert ("renamed", "app.models.UserRepository.get_user") in breaking
    assert r["summary"]["files"] == 4
    assert git_kernel.semantic_diff("no-such-ref")["ok"] is False
    assert git_kernel.semantic_diff("--output=x")["ok"] is False


async def test_semantic_diff_over_mcp(git_kernel: AetherKernel) -> None:
    (git_kernel.root / "app" / "extra.py").write_text("def brand_new() -> int:\n    return 1\n", encoding="utf-8")
    async with Client(build_server(git_kernel)) as client:
        res = await client.call_tool("semantic_diff", {"ref": "HEAD"})
    assert res.data["summary"]["added"] == 1 and res.data["files"][0]["path"] == "app/extra.py"


def test_semantic_diff_outside_git(kernel: AetherKernel) -> None:
    r = kernel.semantic_diff()
    assert r["ok"] is False and "not a git repository" in r["error"]


# ---------------------------------------------------------------------------- living docs


def test_docs_render_mermaid_and_endpoint_table(kernel: AetherKernel) -> None:
    r = kernel.generate_docs()
    assert r["ok"] and r["path"] == "docs/AETHER_DOCS.md" and r["endpoints"] >= 4
    md = (kernel.root / "docs" / "AETHER_DOCS.md").read_text(encoding="utf-8")
    assert "```mermaid\ngraph LR" in md and "subgraph m_backend__repo" in md and "subgraph m_frontend__repo" in md
    assert "m_frontend__src_api -. HTTP" in md and "-> m_backend__app" in md.replace(".->", "->")
    assert "| `POST` | `/api/payments` | `app.api.create_payment` | backend:app/api.py:" in md
    assert "frontend:src/api/client.ts" in md and "| `GET` | `/health` |" in md
    kernel.save_policy(Policy(read_only=["docs/**"]))
    blocked = kernel.generate_docs()
    assert blocked["ok"] is False and "read-only" in blocked["error"]
    assert kernel.generate_docs(write=False)["markdown"].startswith("# backend")
