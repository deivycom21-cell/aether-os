"""Time-travel snapshots, team brain sync, complexity router and git-worktree swarm orchestration."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aether_core.git_sync import load_team_brain
from aether_core.kernel import AetherKernel
from aether_swarm.orchestrator import SwarmError, SwarmOrchestrator, parse_file_blocks, safe_write
from aether_swarm.router import ComplexityRouter, OllamaProbe, Tier
from tests.conftest import fake_secret, git, requires_git


class StaticOllama(OllamaProbe):
    """Probe with a fixed answer, standing in for a running/stopped Ollama daemon."""

    def __init__(self, models: list[str] | None) -> None:
        super().__init__("http://127.0.0.1:9")
        self._fixed = models

    def models(self) -> list[str] | None:
        return self._fixed


# ---------------------------------------------------------------------------- snapshots


def test_snapshot_rollback_restores_exact_state(kernel: AetherKernel) -> None:
    root = kernel.root
    api, models = root / "app" / "api.py", root / "app" / "models.py"
    api_before, models_before = api.read_text(encoding="utf-8"), models.read_bytes()
    snap = kernel.create_snapshot("baseline")
    assert snap["file_count"] >= 7 and snap["new_objects"] == snap["file_count"]
    again = kernel.create_snapshot("no changes")
    assert again["new_objects"] == 0  # content-addressed: unchanged files are not copied twice

    api.write_text("raise SystemExit('oops')\n", encoding="utf-8")
    models.unlink()
    (root / "app" / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    diff = kernel.snapshots.diff(snap["id"])
    assert diff == {"modified": ["app/api.py"], "removed": ["app/models.py"], "added": ["app/new_module.py"]}

    result = kernel.rollback_snapshot(snap["id"])
    assert api.read_text(encoding="utf-8") == api_before and models.read_bytes() == models_before
    assert not (root / "app" / "new_module.py").exists()
    assert result["restored"] == 2 and result["deleted_new_files"] == 1
    safety = result["safety_snapshot"]
    assert safety and any(s["id"] == safety for s in kernel.snapshots.list())
    # The safety snapshot lets us travel forward again.
    kernel.rollback_snapshot(safety)
    assert api.read_text(encoding="utf-8") == "raise SystemExit('oops')\n" and (root / "app" / "new_module.py").exists()
    assert kernel.db.file_by_path("app/new_module.py") is not None  # graph re-indexed after rollback


def test_snapshot_ids_are_validated(kernel: AetherKernel) -> None:
    for bad in ("../x", "..\\x", "", ".hidden"):
        with pytest.raises(ValueError):
            kernel.snapshots.load_manifest(bad)
    with pytest.raises(FileNotFoundError):
        kernel.snapshots.load_manifest("20990101-000000-ffff")


@requires_git
def test_snapshot_uses_git_file_list(git_kernel: AetherKernel) -> None:
    snap = git_kernel.create_snapshot("git")
    manifest = git_kernel.snapshots.load_manifest(snap["id"])
    paths = {e["path"] for e in manifest["files"]}
    assert "app/api.py" in paths and "debug.log" not in paths and not any(p.startswith(".aether") for p in paths)


# ---------------------------------------------------------------------------- team brain


def test_team_brain_roundtrip_never_leaks_secrets(kernel: AetherKernel, tmp_path: Path) -> None:
    key = fake_secret("sk-proj-", 40)
    kernel.db.add_scar("app/payments.py", f"Gateway rejected key {key}", "Rotate keys monthly", "high")
    kernel.db.add_decision("Use idempotency keys", "Every payment call carries an idempotency key.", ["payments"])
    stats = kernel.export_brain()
    brain = kernel.team_brain_path
    assert brain.is_file() and stats["scars"] == 1 and stats["decisions"] == 1 and stats["secrets_masked"] >= 1
    raw = brain.read_bytes()
    assert key.encode() not in raw
    doc = load_team_brain(brain)
    assert {f["path"] for f in doc["files"]} >= {"app/api.py", "src/api/client.ts"}
    assert all("body" not in n for f in doc["files"] for n in f["nodes"])  # no source bodies are shared
    kernel.export_brain()
    assert brain.read_bytes() == raw  # deterministic output -> clean git diffs

    other_root = tmp_path / "teammate"
    other_root.mkdir()
    teammate = AetherKernel(other_root)
    result = teammate.import_brain(brain)
    assert result["scars_imported"] == 1 and result["decisions_imported"] == 1
    assert teammate.import_brain(brain)["scars_imported"] == 0  # idempotent merge
    assert teammate.db.list_decisions()[0]["tags"] == ["payments"]
    teammate.close()
    assert ".aether" in str(kernel.team_brain_path)
    assert "team_brain.json.gz" in (kernel.aether_dir / ".gitignore").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------- router


def test_router_tiers() -> None:
    no_ollama = ComplexityRouter(StaticOllama(None))
    docs = no_ollama.route("Fix typos in the README docs and format comments")
    assert docs.tier == Tier.FAST_MODEL and docs.model == "claude-haiku-5-5"
    local = ComplexityRouter(StaticOllama(["qwen2.5-coder:7b"])).route("Fix typos in the README docs and format comments")
    assert local.tier == Tier.LOCAL_OLLAMA and local.model == "qwen2.5-coder:7b" and local.est_cost_usd == 0.0
    feature = no_ollama.route("Add pagination to the users endpoint and update the handler tests")
    assert feature.tier in {Tier.FAST_MODEL, Tier.BALANCED_MODEL}
    arch = no_ollama.route(
        "Redesign the authentication architecture for multi-repo zero-downtime migration with distributed "
        "session replication, threat model and concurrency guarantees across services/auth.py, api/gateway.ts"
    )
    assert arch.tier == Tier.ARCHITECT_MODEL and arch.model == "claude-opus-5-5" and arch.savings_usd == 0.0
    assert docs.savings_usd > 0 and docs.to_dict()["savings_pct"] > 90
    agg = no_ollama.route_many(["format code", "design a distributed consensus architecture for sharding"])
    assert agg["savings_usd"] > 0 and len(agg["decisions"]) == 2


def test_router_respects_model_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_FAST_MODEL", "claude-sonnet-5-5")
    d = ComplexityRouter(StaticOllama(None)).route("rename a variable")
    assert d.model == "claude-sonnet-5-5"


def test_ollama_probe_handles_unreachable_daemon() -> None:
    probe = OllamaProbe("http://127.0.0.1:9", ttl=0)
    assert probe.models() is None and probe.available() is False


# ---------------------------------------------------------------------------- orchestrator helpers


def test_file_block_parser_and_safe_write(tmp_path: Path) -> None:
    text = "intro\n<<<FILE app/new.py>>>\nprint('hi')\n<<<END>>>\n<<<FILE tests/test_new.py>>>\ndef test_x():\n    assert 1\n<<<END>>>"
    blocks = parse_file_blocks(text)
    assert [b[0] for b in blocks] == ["app/new.py", "tests/test_new.py"]
    safe_write(tmp_path, "app/new.py", blocks[0][1])
    assert (tmp_path / "app" / "new.py").read_text(encoding="utf-8") == "print('hi')\n"
    for bad in ("../escape.py", "/etc/passwd", ".git/config", "a/../../b.py"):
        with pytest.raises(SwarmError):
            safe_write(tmp_path, bad, "x")


def test_swarm_requires_git(kernel: AetherKernel) -> None:
    # The tmp project has no commits (it may or may not sit inside some outer git work tree).
    with pytest.raises(SwarmError, match="git|commit"):
        SwarmOrchestrator(kernel, ComplexityRouter(StaticOllama(None))).launch("Add a feature")


# ---------------------------------------------------------------------------- full swarm lifecycle


@requires_git
def test_swarm_lifecycle_launch_verify_merge_cleanup(git_kernel: AetherKernel, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AETHER_SWARM_TEST_CMD", f'"{sys.executable}" -c "import app.discounts as d; assert d.apply_discount(100, 10) == 90"')
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    orch = SwarmOrchestrator(git_kernel, ComplexityRouter(StaticOllama(None)))
    run = orch.launch("Add a discount calculation helper for payments", roles=("architect", "builder", "tester"))
    rid = run["id"]
    for role in ("architect", "builder", "tester"):
        st = run["roles"][role]
        assert Path(st["worktree"]).is_dir() and Path(st["brief"]).is_file()
        assert st["routing"]["tier"] in {t.value for t in Tier}
    brief = Path(run["roles"]["builder"]["brief"]).read_text(encoding="utf-8")
    assert "Add a discount calculation helper" in brief and "app/payments.py" in brief

    # Without credentials, execution hands the worktrees to manual agents instead of failing.
    assert orch.execute(rid)["status"] == "awaiting_agents"

    # An agent works in the builder worktree and commits.
    builder_wt = Path(run["roles"]["builder"]["worktree"])
    (builder_wt / "app" / "discounts.py").write_text(
        'def apply_discount(amount: int, pct: int) -> int:\n    return amount - amount * pct // 100\n', encoding="utf-8")
    git(builder_wt, "add", "-A")
    git(builder_wt, "commit", "-q", "-m", "add discounts")
    status = orch.status(rid)
    assert status["roles"]["builder"]["commits"] == 1 and status["roles"]["builder"]["changed_files"] == ["app/discounts.py"]

    verified = orch.verify(rid)
    v = verified["verification"]
    assert verified["status"] == "verified", v
    assert v["merged_roles"] == ["builder"] and v["tests"]["passed"] and v["changed_files"] == ["app/discounts.py"]

    merged = orch.merge(rid)
    assert merged["status"] == "merged" and (git_kernel.root / "app" / "discounts.py").is_file()
    assert git_kernel.db.file_by_path("app/discounts.py") is not None
    cleaned = orch.cleanup(rid)
    assert all(not Path(p).exists() for p in cleaned["removed_worktrees"])
    assert f"aether/{rid}/builder" not in git(git_kernel.root, "branch", "--list")


@requires_git
def test_swarm_verification_blocks_secrets_and_failing_tests(git_kernel: AetherKernel, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_SWARM_TEST_CMD", f'"{sys.executable}" -c "raise SystemExit(3)"')
    orch = SwarmOrchestrator(git_kernel, ComplexityRouter(StaticOllama(None)))
    run = orch.launch("Add stripe integration", roles=("builder",))
    wt = Path(run["roles"]["builder"]["worktree"])
    (wt / "app" / "stripe_client.py").write_text(f'API_KEY = "{fake_secret("sk_" + "live_", 24)}"\n', encoding="utf-8")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "stripe")
    result = orch.verify(run["id"])
    v = result["verification"]
    assert result["status"] == "verification_failed"
    assert v["secrets"] and v["secrets"][0]["rule"] == "stripe_key" and v["tests"]["passed"] is False
    with pytest.raises(SwarmError, match="not verified"):
        orch.merge(run["id"])
    orch.cleanup(run["id"])
