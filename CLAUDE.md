# CLAUDE.md — AetherOS working agreement

AetherOS (`aether`) is a **local-first** kernel for AI coding agents: multi-repo code graph in SQLite,
context compression, secret firewall, scars (bug memory), time-travel snapshots, a git-worktree swarm
orchestrator, a FastMCP server, a 3D web dashboard and a Textual TUI.

## Non-negotiable rules

1. **$0 infrastructure.** Everything runs on `localhost`. No hosted services, no telemetry leaving the
   machine, no model downloads. LLM calls are BYOK (env vars / local Ollama) and optional.
2. **Never persist or emit raw secrets.** Every string that leaves the kernel towards an agent, the team
   brain or the dashboard goes through `SecretVault.mask/mask_obj`. The vault lives in RAM only.
3. **Windows-first portability.** Use `pathlib.Path`, explicit `encoding="utf-8"`, `as_posix()` for stored
   paths, no shell scripts, no POSIX-only calls. Subprocesses use argument lists, never `shell=True`.
4. **No placeholders.** No `TODO`, bare `pass` bodies, fake data or mocks in production code.
5. **Telemetry never breaks the caller.** Event publishing / persistence failures are swallowed.
6. **Destructive operations are reversible.** Rollback takes a safety snapshot first; swarm merges
   snapshot first; nothing writes outside the project root.

## Commands (PowerShell, no activation needed)

```powershell
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest              # full suite (~1 min, needs git on PATH for swarm tests)
.\.venv\Scripts\python.exe -m pytest tests\test_security_firewall.py -q
.\.venv\Scripts\aether.exe init                   # index this repo
.\.venv\Scripts\aether.exe query "how are secrets masked"
.\.venv\Scripts\aether.exe web --open             # http://127.0.0.1:7777
.\.venv\Scripts\python.exe benchmarks\run_benchmark.py
node --check <extracted module script>            # optional JS syntax check of the dashboard
```

## Layout

```
aether_core/   kernel.py (facade) · database.py · ast_indexer.py · context_engine.py · security.py
               event_bus.py · git_sync.py · mcp_server.py · cli.py
aether_swarm/  router.py (tiers + Ollama probe) · llm.py (BYOK Anthropic SDK / Ollama) · orchestrator.py
aether_web/    server.py (FastAPI + /ws/telemetry) · static/index.html (Three.js + Canvas2D fallback)
aether_tui/    app.py (Textual)
plugins/       base.py (contract + safe loader) · docker_inspector · git_history_scars · env_shield
benchmarks/    run_benchmark.py
tests/         5 suites, fixtures build a FastAPI backend + Next.js frontend in tmp dirs
```

`AetherKernel` is the only object UIs/servers should talk to. Modules below it must not import UIs.
Dependency direction: `cli/mcp/web/tui -> kernel -> {context_engine, ast_indexer, git_sync, security, event_bus} -> database`.

## Database schema (`.aether/brain.db`, WAL mode)

| table | purpose |
|---|---|
| `meta` | `schema_version`, `index_version` (bumped after every index pass; invalidates vector cache) |
| `repos` | primary + linked repositories (`name` unique, `root_path`, `is_primary`) |
| `files` | `(repo_id, path)` unique, language, sha1, size, line_count, token_estimate |
| `nodes` | symbols: module/class/function/method/route/api_call/interface/section/service/env_var/... with signature, docstring, body (≤4000 chars), lines, JSON metadata |
| `nodes_fts` | FTS5 (`unicode61`, `_` as token char) over name/qualname/signature/docstring/body/path + identifier sub-words |
| `edges` | `imports`, `calls`, `extends`, `http_call` (frontend fetch → backend route), `depends_on`, `builds`; `dst_file_id`/`dst_node_id` filled by `GraphResolver` |
| `scars` | bug memory, `fingerprint` unique (path+symbol+summary+commit), severity, source (`manual`/`git_history`/`team`), hit_count |
| `decisions` | architecture decisions (shared through the team brain) |
| `telemetry` | per-query raw/compressed/saved tokens, USD, latency |
| `events` | event bus persistence; other processes tail it (dashboard ⇄ MCP server) |
| `security_findings` | secret/injection/env findings (previews only, never values) |
| `snapshots` | metadata; manifests in `.aether/snapshots/<id>/manifest.json`, blobs in `_objects/` (sha256) |
| `swarm_runs` | orchestrator state (JSON) |

## Conventions

- Python ≥3.10, `from __future__ import annotations`, full type hints, dataclasses with `slots=True`.
- Stored paths are repo-relative POSIX (`app/api.py`); event node keys are `"<repo>:<path>"`.
- Public kernel/MCP functions return JSON-serialisable dicts; errors are returned as `{"error": ...}`
  or `{"ok": False, ...}` rather than raised across the MCP boundary.
- Tests never contain literal credential-shaped strings: use `tests.conftest.fake_secret()`.
- Event types live in `aether_core.event_bus.EventType`; the dashboard colours are keyed by them.
- Prices for savings math: `MODEL_PRICES_PER_MTOK` in `context_engine.py` (input $/MTok:
  Haiku 5.5 $0.10, Sonnet 5.5 $2, Opus 5.5 $4).

## Gotchas

- Windows `shlex.split(posix=False)` keeps quotes — `detect_test_command` strips them.
- `ProcessPoolExecutor` parsing kicks in at ≥64 changed files (`AETHER_NO_PARALLEL=1` disables it).
- `.aether/.gitignore` ignores everything except `team_brain.json.gz`; swarm merges ignore `.aether/` dirt.
- Plugin-managed file records (e.g. `.env.example` names) survive re-indexing while the file exists.
