"""v0.2 · #1 auto-fix loop (heal) and #2 living rules (sync-rules)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp import Client

from aether_core.healer import Healer, parse_failure, split_command
from aether_core.kernel import AetherKernel
from aether_core.mcp_server import build_server
from aether_core.policy import Policy
from aether_core.rules_sync import END_MARKER, START_MARKER, extract_block, upsert_block
from tests.conftest import fake_secret

PYTEST_CMD = "python -m pytest -q -p no:cacheprovider tests/test_negative.py"
FAILING_TEST = '''from app.payments import charge_customer
from app.models import User


def test_negative_amount_is_refunded():
    assert charge_customer(User(1, "a@b.c"), -5)["status"] == "refunded"
'''
FIXED_PAYMENTS = '''"""Payment processing with retry logic."""
from .models import User


def charge_customer(user: User, amount_cents: int, currency: str = "usd") -> dict:
    """Charge a customer; negative amounts are refunds."""
    status = "refunded" if amount_cents < 0 else "charged"
    return {"user": user.id, "amount": amount_cents, "currency": currency, "status": status}
'''


@dataclass
class FakeResponse:
    text: str
    model: str = "fake-model"


class ScriptedLLM:
    """Test double for the BYOK client: replays scripted answers and records prompts."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.prompts: list[str] = []

    def available(self) -> dict[str, bool]:
        return {"anthropic": True, "ollama": False}

    def complete(self, decision: Any, system: str, prompt: str, max_tokens: int = 16000) -> FakeResponse:
        self.prompts.append(prompt)
        return FakeResponse(self.answers[min(len(self.prompts), len(self.answers)) - 1])


def _write_failing_test(kernel: AetherKernel) -> None:
    (kernel.root / "tests" / "test_negative.py").write_text(FAILING_TEST, encoding="utf-8")


# ---------------------------------------------------------------------------- parsing


def test_parse_failure_understands_python_pytest_node_and_tsc(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    out = "\n".join([
        "Traceback (most recent call last):",
        f'  File "{(tmp_path / "app" / "main.py").as_posix()}", line 3, in <module>',
        '  File "/usr/lib/python3.12/site-packages/lib.py", line 9, in helper',
        '  File "app/core.py", line 12, in compute',
        "ValueError: amount must be positive",
        "tests/test_x.py:7: AssertionError",
        "    at fetchUser (src/api/client.ts:8:11)",
        "src/utils/format.ts(3,5): error TS2322: Type 'string' is not assignable to type 'number'.",
    ])
    r = parse_failure(out, tmp_path)
    files = [(f["file"], f["line"]) for f in r["frames"]]
    assert files[0] == ("app/core.py", 12)  # innermost python frame first
    assert ("app/main.py", 3) in files and ("tests/test_x.py", 7) in files
    assert ("src/api/client.ts", 8) in files and ("src/utils/format.ts", 3) in files
    assert not any("site-packages" in f for f, _ in files)
    assert any(e.startswith("ValueError: amount must be positive") for e in r["errors"])
    assert split_command('pytest -k "a b"') in (["pytest", "-k", "a b"],)


# ---------------------------------------------------------------------------- prepare


def test_heal_prepares_context_with_frames_scars_and_masking(kernel: AetherKernel) -> None:
    _write_failing_test(kernel)
    secret = fake_secret("ghp_", 36)
    kernel.record_scar("app/payments.py", "Refunds were charged twice", "Use idempotency keys", "critical")
    ctx = kernel.prepare_heal_context(PYTEST_CMD)
    assert ctx["status"] == "failing" and ctx["returncode"] != 0
    assert ctx["context_file"] == ".aether/last_heal_context.json"
    assert any(f["file"] == "app/payments.py" and f["symbol"].endswith("charge_customer") for f in ctx["frames"])
    assert any("ValueError" in e for e in ctx["errors"])
    assert ctx["scars"] and ctx["scars"][0]["severity"] == "critical"
    assert ctx["context"]["results"], "smart context should be attached"
    on_disk = json.loads((kernel.aether_dir / "last_heal_context.json").read_text(encoding="utf-8"))
    assert on_disk["frames"][0]["snippet"]
    # output that carries a secret is masked before it is persisted or returned
    leaked = kernel.prepare_heal_context(output=f"RuntimeError: token {secret} rejected\n")
    assert secret not in json.dumps(leaked) and secret not in (kernel.aether_dir / "last_heal_context.json").read_text(encoding="utf-8")
    assert leaked["status"] == "failing"


def test_heal_reports_passing_and_enforces_command_policy(kernel: AetherKernel) -> None:
    ok = kernel.prepare_heal_context("python -m pytest -q -p no:cacheprovider tests/test_payments.py")
    assert ok["status"] == "passing" and ok["returncode"] == 0
    refused = kernel.prepare_heal_context("git push --force")
    assert refused["ok"] is False and "not allowed" in refused["error"]
    kernel.save_policy(Policy(allowed_commands=["git status"]))
    assert kernel.prepare_heal_context(PYTEST_CMD)["ok"] is False
    seen = {e["type"] for e in kernel.db.recent_events(30)}
    assert {"HEAL_CONTEXT", "POLICY_BLOCKED"} <= seen


# ---------------------------------------------------------------------------- execute (BYOK loop)


def test_heal_execute_repairs_under_snapshot_and_records_scar(kernel: AetherKernel) -> None:
    _write_failing_test(kernel)
    llm = ScriptedLLM([f"<<<FILE app/payments.py>>>\n{FIXED_PAYMENTS}\n<<<END>>>"])
    r = Healer(kernel).execute(PYTEST_CMD, llm=llm)
    assert r["ok"] and r["status"] == "healed" and r["files_changed"] == ["app/payments.py"]
    assert r["snapshot"] and len(r["attempts"]) == 1
    assert "refunded" in (kernel.root / "app" / "payments.py").read_text(encoding="utf-8")
    assert "app/payments.py" in llm.prompts[0] and "ValueError" in llm.prompts[0]
    scars = kernel.check_scars("app/payments.py")
    assert any(s["error_summary"].startswith("Auto-healed failure") for s in scars)


def test_heal_execute_rolls_back_after_three_failed_attempts(kernel: AetherKernel) -> None:
    _write_failing_test(kernel)
    original = (kernel.root / "app" / "payments.py").read_text(encoding="utf-8")
    broken = "<<<FILE app/payments.py>>>\ndef charge_customer(user, amount_cents, currency='usd'):\n    raise ValueError('still broken')\n<<<END>>>"
    llm = ScriptedLLM([broken])
    r = Healer(kernel).execute(PYTEST_CMD, llm=llm)
    assert r["ok"] is False and r["rolled_back"] and len(r["attempts"]) == 3 and len(llm.prompts) == 3
    assert (kernel.root / "app" / "payments.py").read_text(encoding="utf-8") == original


def test_heal_execute_refuses_secrets_and_policy_protected_files(kernel: AetherKernel) -> None:
    _write_failing_test(kernel)
    kernel.save_policy(Policy(read_only=["app/payments.py"]))
    leak = fake_secret("sk-ant-api03-", 40)
    llm = ScriptedLLM([f"<<<FILE app/config.py>>>\nKEY = '{leak}'\n<<<END>>>",
                       f"<<<FILE app/payments.py>>>\n{FIXED_PAYMENTS}\n<<<END>>>",
                       "<<<FILE ../outside.py>>>\nx = 1\n<<<END>>>"])
    r = Healer(kernel).execute(PYTEST_CMD, llm=llm)
    errors = [a.get("error", "") for a in r["attempts"]]
    assert r["ok"] is False
    assert "secret-like" in errors[0] and "read-only" in errors[1] and "outside" in errors[2]
    assert not (kernel.root / "app" / "config.py").exists() and not (kernel.root.parent / "outside.py").exists()


def test_heal_execute_without_backend_explains_byok(kernel: AetherKernel) -> None:
    class Offline(ScriptedLLM):
        def available(self) -> dict[str, bool]:
            return {"anthropic": False, "ollama": False}

    r = kernel.heal(PYTEST_CMD, execute=True, llm=Offline([]))
    assert r["ok"] is False and "ANTHROPIC_API_KEY" in r["error"]


async def test_mcp_prepare_heal_context_tool(kernel: AetherKernel) -> None:
    output = 'Traceback (most recent call last):\n  File "app/payments.py", line 7, in charge_customer\nValueError: amount must be positive\n'
    async with Client(build_server(kernel)) as client:
        res = await client.call_tool("prepare_heal_context", {"output": output})
    data = res.data
    assert data["frames"][0]["file"] == "app/payments.py" and data["frames"][0]["symbol"].endswith("charge_customer")


# ---------------------------------------------------------------------------- living rules


def test_upsert_block_preserves_user_content_and_line_endings() -> None:
    manual = "# My rules\r\n\r\nNever touch prod.\r\n"
    first = upsert_block(manual, "## Generated\nline")
    assert first.startswith(manual) and "\r\n## Generated\r\nline\r\n" in first and first.count(START_MARKER) == 1
    second = upsert_block(first + "Footer kept\r\n", "## Generated v2")
    assert "line" not in extract_block(second) and second.endswith("Footer kept\r\n") and "Never touch prod." in second
    assert upsert_block("", "x") == f"{START_MARKER}\nx\n{END_MARKER}\n"


def test_sync_rules_injects_live_facts_without_destroying_manual_rules(kernel: AetherKernel) -> None:
    claude = kernel.root / "CLAUDE.md"
    claude.write_text("# Team rules\n\nAlways write tests.\n", encoding="utf-8")
    kernel.record_scar("app/payments.py", "Double charge on retry", "Idempotency key", "critical")
    kernel.db.add_decision("Use FastAPI", "All HTTP services use FastAPI routers.", ["backend"])
    r = kernel.sync_rules()
    actions = {f["path"]: f["action"] for f in r["files"]}
    assert actions == {"CLAUDE.md": "updated", ".cursorrules": "created"}
    text = claude.read_text(encoding="utf-8")
    assert text.startswith("# Team rules\n\nAlways write tests.\n")
    block = extract_block(text)
    assert block and "POST /api/payments" in block and "Double charge on retry" in block and "Use FastAPI" in block
    assert "`app/`" in block and "frontend" in block
    assert extract_block((kernel.root / ".cursorrules").read_text(encoding="utf-8")) == block
    again = kernel.sync_rules()
    assert {f["action"] for f in again["files"]} == {"unchanged"}
    kernel.save_policy(Policy(read_only=["CLAUDE.md"]))
    refused = kernel.sync_rules(["CLAUDE.md"])
    assert refused["files"][0]["action"] == "refused"
