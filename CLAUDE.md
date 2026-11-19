# CLAUDE.md — AetherOS working agreement

AetherOS (`aether`) is a **local-first** kernel for AI coding agents: multi-repo code graph in SQLite,
context compression, secret firewall, scars (bug memory), time-travel snapshots, a git-worktree swarm
orchestrator, a FastMCP server, a 3D web dashboard and a Textual TUI. v0.2.0 adds the auto-fix loop,
living rules/docs, hotspots, semantic diff, dead-code detection, test skeletons, episodic checkpoints,
a permission policy, a pre-commit guard, a context-budget profiler and three plugins.

## Non-negotiable rules

1. **$0 infrastructure.** Everything runs on `localhost`. No hosted services, no telemetry leaving the
   machine, no model downloads. LLM calls are BYOK (env vars / local Ollama) and optional.
2. **Never persist or emit raw secrets.** Every string that leaves the kernel towards an agent, the team
   brain or the dashboard goes through `SecretVault.mask/mask_obj`. The vault lives in RAM only.
3. **Windows-first portability.** Use `pathlib.Path`, explicit `encoding="utf-8"`, `as_posix()` for stored
   paths, no shell scripts, no POSIX-only calls. Subprocesses use argument lists, never `shell=True`.
   Sole exception: the thin bootstrap installers `install.ps1` / `install.sh` (install uv + the package,
   then call `aether setup`); all setup logic lives in Python (`setup_wizard.py`).
4. **No placeholders.** No `TODO`, bare `pass` bodies, fake data or mocks in production code.
5. **Telemetry never breaks the caller.** Event publishing / persistence failures are swallowed.
6. **Destructive operations are reversible.** Rollback takes a safety snapshot first; swarm merges and
   `heal --execute` snapshot first (heal rolls back on failure); nothing writes outside the project root.
7. **Respect `.aether/policy.json`.** Anything that exposes file content checks `policy.can_read`; anything
   that writes checks `policy.can_write`; anything that runs a command checks `policy.command_allowed`.

## Commands (PowerShell, no activation needed)

```powershell
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest              # full suite (118 tests, ~5 min on Windows; needs git on PATH)
.\.venv\Scripts\python.exe -m pytest tests\test_security_firewall.py -q
.\.venv\Scripts\aether.exe setup                  # one-command onboarding: index, MCP, hook, rules, live check
.\.venv\Scripts\aether.exe init                   # index this repo
.\.venv\Scripts\aether.exe query "how are secrets masked"
.\.venv\Scripts\aether.exe web --open             # http://127.0.0.1:7777
.\.venv\Scripts\python.exe benchmarks\run_benchmark.py
node --check <extracted module script>            # optional JS syntax check of the dashboard
# v0.2.0
.\.venv\Scripts\aether.exe hotspots / dead-code / semantic-diff / docs / budget / sync-rules / share
.\.venv\Scripts\aether.exe heal "python -m pytest -x"       # writes .aether/last_heal_context.json
.\.venv\Scripts\python.exe -m pytest tests\test_v02_tooling.py -q   # installer, hooks, gen-tests, CLI smoke
```

## Layout

```
aether_core/   kernel.py (facade) · database.py · ast_indexer.py · context_engine.py · security.py
               event_bus.py · git_sync.py · mcp_server.py · cli.py
               v0.2: policy.py · healer.py · rules_sync.py · share_card.py · mcp_install.py · hotspots.py
               sessions.py · budget.py · precommit.py · hooks.py · semantic_diff.py · dead_code.py
               test_gen.py · docs_gen.py · setup_wizard.py (IDE detection + live MCP handshake)
aether_swarm/  router.py (tiers + Ollama probe) · llm.py (BYOK Anthropic SDK / Ollama) · orchestrator.py
aether_web/    server.py (FastAPI + /ws/telemetry) · static/index.html (Three.js + Canvas2D fallback)
aether_tui/    app.py (Textual)
plugins/       base.py (contract + safe loader) · docker_inspector · git_history_scars · env_shield
               v0.2: db_schema_inspector · runtime_log_watcher · dep_auditor
benchmarks/    run_benchmark.py
install.ps1 / install.sh   one-line bootstrap (uv -> uv tool install -> aether setup)
tests/         10 suites (5 original + 5 test_v02_*), fixtures build a FastAPI backend + Next.js frontend in tmp dirs
```

`AetherKernel` is the only object UIs/servers should talk to. Modules below it must not import UIs.
Dependency direction: `cli/mcp/web/tui -> kernel -> {context_engine, ast_indexer, git_sync, security, event_bus} -> database`.

## Database schema (`.aether/brain.db`, WAL mode)

| table | purpose |
|---|---|
| `meta` | `schema_version` (format contract, stays 1), `schema_revision` (additive changes, 2 in v0.2), `index_version` (bumped after every index pass; invalidates vector cache) |
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
| `session_checkpoints` | v0.2 episodic memory: goal, files, pending, decisions, notes, summary (≤400 tokens) |
| `budget_usage` | v0.2 context budget: tokens per MCP response by session/tool + files/query detail |

## Conventions

- Python ≥3.10, `from __future__ import annotations`, full type hints, dataclasses with `slots=True`.
- Stored paths are repo-relative POSIX (`app/api.py`); event node keys are `"<repo>:<path>"`.
- Public kernel/MCP functions return JSON-serialisable dicts; errors are returned as `{"error": ...}`
  or `{"ok": False, ...}` rather than raised across the MCP boundary.
- Tests never contain literal credential-shaped strings: use `tests.conftest.fake_secret()`.
- Event types live in `aether_core.event_bus.EventType`; the dashboard colours are keyed by them.
- Prices for savings math: `MODEL_PRICES_PER_MTOK` in `context_engine.py` (input $/MTok:
  Haiku 5.5 $0.10, Sonnet 5.5 $2, Opus 5.5 $4).
- New MCP tools go through `guard(obj, tool, query)` in `mcp_server.py`: mask first, then meter the budget.
- Plugin-owned synthetic file records use the `@aether/` prefix (`VIRTUAL_PREFIX`); the plugin prunes them.
- Bulk filters take one policy snapshot (`pol = self.policy`) instead of hitting the mtime check per item.
- Tests that need prompt-injection payloads assemble them at runtime (see `tests/test_v02_tooling.py`) so
  the pre-commit guard never flags the test suite itself.

## Gotchas

- Windows `shlex.split(posix=False)` keeps quotes — `detect_test_command` strips them.
- `ProcessPoolExecutor` parsing kicks in at ≥64 changed files (`AETHER_NO_PARALLEL=1` disables it).
- `.aether/.gitignore` ignores everything except `team_brain.json.gz`; swarm merges ignore `.aether/` dirt.
- Plugin-managed file records (e.g. `.env.example` names) survive re-indexing while the file exists.
- Rich parses `[...]` as markup: wrap dynamic text in `rich.markup.escape(...)` or `Text(...)` in the CLI.
- The pre-commit hook runs the *base* interpreter with `-S` and `PYTHONPATH`; `aether_core.precommit` must
  stay stdlib-only (no Typer/Rich/kernel imports). The scan is ~30 ms; total hook time is dominated by
  CPython start-up and `git diff` on Windows.
- CLI graph analyses (`hotspots`, `dead-code`, `docs`, `sync-rules`) re-index incrementally first; the kernel
  methods do not, so call `kernel.index()` in tests after writing files.
- `find_project_root` never climbs to the home folder or a drive root (dotfiles repos in `~` exist);
  `aether setup` also refuses folders without project markers unless `--here` is passed.
- `aether setup`'s live handshake spawns the configured MCP command; a cold uv tool env can take ~45 s.
- MCP `semantic_diff` is compact (signatures clipped, body edits counted, ≤25 files) to protect the budget.

<!-- AETHER:START -->
## AetherOS live context

_Auto-generated by `aether sync-rules` on 2026-11-18. Edits inside this block are overwritten; write your own rules outside the markers._

- **Project:** `aether-os`
- **Graph:** 53 code files · 872 symbols · 1071 resolved code edges · 21 HTTP routes · 0 scars

### Architecture (modules by size)

- `aether_core/` (aether-os) — 24 files, 500 symbols, python; uses `aether_swarm`, `plugins`, `aether_web`, `aether_tui`
- `tests/` (aether-os) — 12 files, 154 symbols, python; uses `aether_core`, `plugins`, `aether_swarm`, `aether_web`, `aether_tui`
- `aether_web/` (aether-os) — 2 files, 72 symbols, python; uses `aether_core`
- `plugins/` (aether-os) — 8 files, 68 symbols, python; uses `aether_core`
- `aether_swarm/` (aether-os) — 4 files, 53 symbols, python; uses `aether_core`
- `aether_tui/` (aether-os) — 2 files, 21 symbols, python; uses `aether_core`
- `benchmarks/` (aether-os) — 1 files, 4 symbols, python; uses `aether_core`

### API routes (21)

- `GET /` → `aether_web.server.create_app.index` (aether_web/server.py:86)
- `GET /api/budget` → `aether_web.server.create_app.budget` (aether_web/server.py:166)
- `GET /api/checkpoints` → `aether_web.server.create_app.checkpoints` (aether_web/server.py:174)
- `GET /api/events` → `aether_web.server.create_app.events` (aether_web/server.py:137)
- `GET /api/file` → `aether_web.server.create_app.file_detail` (aether_web/server.py:118)
- `GET /api/graph` → `aether_web.server.create_app.graph` (aether_web/server.py:100)
- `GET /api/hotspots` → `aether_web.server.create_app.hotspots` (aether_web/server.py:162)
- `GET /api/impact` → `aether_web.server.create_app.impact` (aether_web/server.py:154)
- `POST /api/index` → `aether_web.server.create_app.reindex` (aether_web/server.py:201)
- `GET /api/policy` → `aether_web.server.create_app.policy` (aether_web/server.py:170)
- `POST /api/query` → `aether_web.server.create_app.query` (aether_web/server.py:180)
- `GET /api/repos` → `aether_web.server.create_app.repos` (aether_web/server.py:150)
- `POST /api/scan` → `aether_web.server.create_app.scan` (aether_web/server.py:197)
- `GET /api/scars` → `aether_web.server.create_app.scars` (aether_web/server.py:141)
- `GET /api/snapshots` → `aether_web.server.create_app.snapshots` (aether_web/server.py:146)
- `POST /api/snapshots` → `aether_web.server.create_app.create_snapshot` (aether_web/server.py:184)
- `POST /api/snapshots/{snapshot_id}/rollback` → `aether_web.server.create_app.rollback` (aether_web/server.py:188)
- `GET /api/stats` → `aether_web.server.create_app.stats` (aether_web/server.py:96)
- `GET /api/swarm` → `aether_web.server.create_app.swarm` (aether_web/server.py:158)
- `GET /healthz` → `aether_web.server.create_app.healthz` (aether_web/server.py:91)
- `ANY /ws/telemetry` → `aether_web.server.create_app.telemetry` (aether_web/server.py:206)

### Hotspots (highest technical-debt risk)

- `aether_core/kernel.py` risk 77/100 (critical) — complex function semantic_diff (cyclomatic ~19); 19 files depend on it
- `aether_core/ast_indexer.py` risk 75/100 (critical) — complex function _visit_func (cyclomatic ~35); 10 files depend on it
- `aether_core/database.py` risk 73/100 (critical) — complex function fts_search (cyclomatic ~15); 12 files depend on it
- `aether_core/context_engine.py` risk 70/100 (critical) — complex function query (cyclomatic ~35); 9 files depend on it
- `aether_core/security.py` risk 69/100 (high) — complex function analyze (cyclomatic ~21); 8 files depend on it

### Agent workflow (Aether MCP tools)

1. `query_smart_context` before opening large files; `check_scars` + `impact_analysis` before editing.
2. `create_snapshot` before risky multi-file edits; `semantic_diff` to review what changed.
3. On failing tests: `prepare_heal_context`; after the fix: `record_scar`.
4. Before ending a session: `save_session_checkpoint` (resume later with `resume_session`).
<!-- AETHER:END -->
