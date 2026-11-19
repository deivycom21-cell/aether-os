# AETHER//OS

**A local-first memory kernel for AI coding agents.** AetherOS gives Claude Code, Cursor, Windsurf and
Aider a multi-repo code graph, compressed context, a secret firewall, a memory of past bugs, time-travel
snapshots, a git-worktree swarm orchestrator — and a live 3D view of your codebase that lights up as
your agent works.

Everything runs on `localhost` with SQLite. No servers, no accounts, no model downloads. Your API keys
stay in your environment.

```
$ aether query "how are secrets masked before reaching the agent"
...
304,928 -> 21,664 tokens (-92.9%) across 15 real queries on this repo · p50 46 ms
```

---

## What's new in v0.2.0

Fifteen new capabilities, all still local-first, $0 infrastructure and BYOK:

| # | Capability | Entry points |
|---|---|---|
| 1 | **Auto-fix loop** — runs your test/build command, parses Python/pytest/Node/tsc/Go/Rust failures, maps frames to symbols, attaches scars + smart context to `.aether/last_heal_context.json`; `--execute` repairs with your own model (max 3 attempts, safety snapshot, rollback on failure) | `aether heal "<cmd>" [--execute]`, MCP `prepare_heal_context` |
| 2 | **Living agent rules** — keeps a `<!-- AETHER:START/END -->` block in `CLAUDE.md` / `.cursorrules` in sync with real modules, routes, decisions, critical scars and hotspots; your own text is never touched | `aether sync-rules` |
| 3 | **Three new plugins** — `db_schema_inspector` (SQL, Prisma, SQLAlchemy/SQLModel/Pydantic, local SQLite — structure only), `runtime_log_watcher` (recurring exceptions in `*.log` → preventive scars), `dep_auditor` (unpinned/URL deps, risky install scripts) | `aether plugins`, `aether index` |
| 4 | **Share card** — self-contained 1200×630 SVG with your real savings, no external assets | `aether share [--out card.svg]` |
| 5 | **Universal MCP installer** — Claude Code, Cursor, Windsurf, Cline/Roo Code (VS Code), Zed (JSONC-safe) and OpenCode | `aether install-mcp --all` / `--target zed` |
| 6 | **Hotspots** — 0-100 risk from complexity (30 %), fan-in (25 %), git churn (25 %) and scars (20 %) | `aether hotspots`, MCP `get_code_hotspots`, `/api/graph` `risk` |
| 7 | **Episodic memory** — compact (<400 tokens) session checkpoints: goal, touched files, pending work, decisions | `aether checkpoint` / `aether resume`, MCP `save_session_checkpoint` / `resume_session` |
| 8 | **Pre-commit guard** — scans only the *added* lines of the staged diff for secrets and prompt injections, chains your existing hook | `aether install-hooks`, `aether scan --staged` |
| 9 | **Semantic diff** — functions/classes/methods/routes added, removed, renamed, signature-changed (flagged as breaking) vs any git ref | `aether semantic-diff [ref]`, MCP `semantic_diff` |
| 10 | **Dashboard upgrades** — fly-to camera on node/search clicks, radar filters `ALL · HOTSPOTS · SCARS · ROUTES` (also `#radar=hotspots` in the URL), optional Web Audio synth (`AUDIO: OFF/ON`, zero audio files) | `aether web` |
| 11 | **Dead code** — zero-reference functions/classes/methods (graph + textual cross-check) and endpoints no client consumes; routes, registered callbacks, tests and CLI entrypoints excluded | `aether dead-code`, MCP `find_dead_code` |
| 12 | **Test skeletons** — runnable pytest (signature locks, smoke calls, FastAPI/Flask route probes) or vitest files | `aether gen-tests <file>` |
| 13 | **Permission sandbox** — `deny`, `read_only`, `allowed_commands`, `scan_ignore` in `.aether/policy.json`, enforced by queries, impact analysis, rollback, heal, docs, rules and the dashboard API | `aether policy`, MCP `check_path_policy` |
| 14 | **Living docs** — Mermaid module-dependency diagram (cross-repo HTTP edges included) + API endpoint table with consumers | `aether docs` |
| 15 | **Context budget profiler** — every MCP response is metered per session, tool, file and query; `BUDGET_WARNING` hits the HUD at 80 % / 100 % / 150 % / 200 % / 300 % | `aether budget`, MCP `get_context_budget` |

---

## Why

Agents waste most of their context window reading entire files to find one function, they repeat bugs
the team already fixed, and they will happily paste a `.env` value into a prompt. AetherOS sits between
the agent and the repo:

| Problem | AetherOS |
|---|---|
| Agent reads 2,000-line files for one function | `query_smart_context` returns signatures, the exact matching fragment, dependencies and dependents within a token budget, and reports the tokens and dollars saved |
| "What breaks if I change this?" | `impact_analysis` follows imports, calls and **cross-repo HTTP calls** (frontend `fetch("/api/x")` → backend route) |
| The same bug comes back | **Scars**: recorded lessons plus scars mined from `fix/revert/crash` commits, injected as warnings when the agent touches that file |
| Secrets leak into prompts | Regex + Shannon-entropy firewall with a reversible in-RAM vault: `<<AETHER_MASKED_SECRET_1>>` |
| Poisoned dependencies / docs | Prompt-injection detector (hidden comments, bidi unicode, base64 payloads, `curl \| sh`) |
| Risky multi-file edits | Content-addressed snapshots with one-command (or one-click) rollback |
| Big features | `aether swarm`: one git worktree per role (architect, builder, tester, security auditor), automatic verification (tests + secret scan) and a safe merge |
| Cost | A router sends formatting/docs work to local Ollama or Haiku, and architecture work to Opus |

## Quickstart

Requires Python ≥ 3.10 and git. Works on Windows, macOS and Linux.

```powershell
# Windows (PowerShell) — no activation scripts needed
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe -e .
.\.venv\Scripts\aether.exe init                       # index the current repo
.\.venv\Scripts\aether.exe install-mcp --all          # Claude Code, Cursor, Windsurf, Cline/Roo, Zed, OpenCode
.\.venv\Scripts\aether.exe install-hooks              # optional: block secrets / injections at commit time
.\.venv\Scripts\aether.exe web --open                 # 3D dashboard at http://127.0.0.1:7777
```

```bash
# macOS / Linux
uv venv && uv pip install -e .        # or: python -m venv .venv && .venv/bin/pip install -e .
aether init --link-repo ../frontend   # index this repo + a sibling repo
aether install-mcp && aether web --open
```

Restart Claude Code in the project: the `aether` MCP server shows up with its tools. To share what
the project knows with your team, commit `.aether/team_brain.json.gz` (`aether export-brain`).

## MCP tools

| Tool | What it does |
|---|---|
| `query_smart_context(query, max_tokens=1500)` | Hybrid FTS5-BM25 + TF-IDF vector search, fused with RRF, then packed into a token budget: signatures, docstrings, matching fragments, deps/dependents, scars, savings metrics |
| `impact_analysis(file_path)` | Transitive dependents, endpoints defined, endpoint consumers in linked repos, risk score |
| `record_scar(file_path, error_summary, fix_applied, severity)` | Remember a bug and its fix |
| `check_scars(file_path)` | Warnings before editing |
| `create_snapshot(label)` / `rollback_snapshot(id)` | Time travel (a safety snapshot is taken before every rollback) |
| `scan_security(target_path=".")` | Secrets + prompt-injection audit (values are never returned) |
| `get_telemetry_stats()` | Tokens and USD saved, graph size, security counters, swarm state, context budget |
| `prepare_heal_context(command="", output="")` | Run an allowed test/build command (or analyse output you already have) and get errors, project frames with code windows, enclosing symbols, scars and smart context in one call |
| `get_code_hotspots(limit=15)` | Files ranked by technical-debt risk with the reasons behind each score |
| `semantic_diff(ref="HEAD", paths=None, max_files=25)` | Symbol-level diff, breaking changes flagged; compact by design for agent context |
| `find_dead_code(min_confidence="medium")` | Orphan symbols and unconsumed endpoints, with confidence levels |
| `save_session_checkpoint(goal, files_touched, pending, decisions, notes)` / `resume_session(id=0)` | Episodic memory across context windows |
| `check_path_policy(path)` | Whether a path is readable/writable under `.aether/policy.json` |
| `get_context_budget()` | Tokens this session has consumed per tool, file and query vs. the budget |

Resources: `aether://telemetry`, `aether://scars`, `aether://graph/summary`, `aether://decisions`,
`aether://policy`, `aether://session/latest`. Every tool response is secret-masked and metered by the
context-budget profiler.

## CLI

```
aether init [--link-repo PATH]...     aether index [--force]          aether link/unlink/repos
aether query "..." [-t 1500] [--json] aether impact FILE              aether scan [PATH]
aether scar FILE "error" --fix "..."  aether scars [FILE]             aether decision TITLE BODY
aether snapshot [LABEL]               aether snapshots / diff ID      aether rollback ID [-y]
aether swarm "feature" [--execute]    aether swarm-status/-execute/-verify/-merge/-cleanup ID
aether route "prompt"                 aether export-brain / import-brain
aether web [--open]   aether tui      aether watch    aether mcp      aether plugins   aether benchmark

# v0.2.0
aether heal "pytest -x" [--execute]   aether sync-rules [-t AGENTS.md]   aether share [--out card.svg]
aether install-mcp --all | -t zed     aether install-hooks [--uninstall] aether scan --staged
aether hotspots [-n 15]               aether semantic-diff [REF] [-p F]  aether dead-code [-c high]
aether gen-tests FILE [--print]       aether docs [--out docs/X.md]      aether budget [--threshold N]
aether checkpoint "goal" -p "next"    aether resume [ID] [--list]        aether policy [--check P] [--read-only G]
```

## The 3D digital twin

`aether web` serves a self-contained page (Three.js from a CDN, with an automatic Canvas 2D fallback
when you're offline or WebGL is unavailable). Files are nodes clustered by repository and directory,
dependencies are edges, and every kernel event — `CONTEXT_QUERY`, `SCAR_TRIGGERED`, `SECRET_BLOCKED`,
`SWARM_STEP`, `SNAPSHOT_CREATED` — sends light pulses from the core to the files involved, live over
`/ws/telemetry`. Events are shared through SQLite, so the dashboard animates what the MCP server
inside Claude Code is doing in another process. Hover a node for details, click it for symbols,
dependencies, scars and impact analysis, and roll back snapshots from the side panel.

v0.2.0 adds a **fly-to camera** (clicking a node or a search hit glides the camera to it), **radar
filters** in the header (`HOTSPOTS` tints the riskiest files red/amber, `SCARS` and `ROUTES` isolate
fragile files and API surface; the choice is mirrored in `#radar=...` so you can share the view), an
optional **Web Audio synthesizer** (`AUDIO: OFF/ON` — every event type has its own synthesized tone,
nothing is downloaded) and a **context-budget gauge** with a flashing alert when `BUDGET_WARNING` fires.

`aether tui` is the terminal version: KPIs, savings sparkline, live event bus, scars and snapshots,
with `i` (re-index), `s` (snapshot) and `x` (scan) key bindings.

## Benchmark (this repository)

`aether benchmark` (or `python benchmarks/run_benchmark.py`) indexes the repo into a throwaway
database and runs 15 realistic queries. Results for v0.2.0 on a Windows 11 laptop, Python 3.12 (the
repository itself grew from 36 to 60 files between v0.1 and v0.2; timings vary with machine load):

| Metric | v0.2.0 | v0.1.0 |
|---|---|---|
| Graph | 60 files · 987 symbols · 4,893 edges | 36 files · 610 symbols · 2,915 edges |
| Cold index / warm re-index | 6,304 ms / 446 ms | 876 ms / 70 ms |
| Query latency | p50 46 ms · p95 80 ms | p50 33 ms · p95 48 ms |
| Tokens (raw files of the hits → AetherOS package) | 304,928 → 21,664 (**92.9% saved**) | 224,297 → 21,000 (90.6%) |
| Input cost saved over the 15 queries | $0.57 Sonnet 5.5 · $1.13 Opus 5.5 | $0.41 · $0.81 |

The permission policy adds ~1.5 ms per query (one policy snapshot per query, decisions memoised per path).

"Raw" counts the full files an agent would otherwise open to see those hits, so the comparison is
conservative for big repos and generous for tiny files. When a package would cost more than the raw
files (tiny repos), the savings are reported as 0 rather than a negative number.

## Swarm orchestration

```powershell
aether swarm "Add a discount engine with per-customer rules"
# -> .aether/worktrees/<run>/{architect,builder,tester,security_auditor} + briefs/<role>.md
#    work in each worktree with your agent (or pass --execute to use your own API key / Ollama)
aether swarm-verify <run>     # merges role branches in an integration worktree, runs tests + secret scan
aether swarm-merge  <run>     # snapshot, then --no-ff merge into your branch
aether swarm-cleanup <run>
```

With `--execute`, roles call the official Anthropic SDK (`pip install "aether-os[llm]"`, reads
`ANTHROPIC_API_KEY`) or a local Ollama at `http://localhost:11434`. Tiers default to `claude-haiku-5-5`,
`claude-sonnet-5-5` and `claude-opus-5-5`; override with `AETHER_FAST_MODEL`, `AETHER_BALANCED_MODEL`,
`AETHER_ARCHITECT_MODEL` and `AETHER_OLLAMA_MODEL`.

## Plugins

Built-in: `docker_inspector` (compose services, ports, running containers), `git_history_scars`
(preventive scars from fix commits), `env_shield` (`.env` vs `.env.example`, leaked example values,
un-ignored env files; only variable **names** are stored), and new in v0.2.0 `db_schema_inspector`
(tables/columns from `.sql`, `schema.prisma`, SQLAlchemy/SQLModel/Pydantic models and local SQLite files
opened read-only — rows are never read), `runtime_log_watcher` (exceptions repeated in local `*.log` files
become preventive scars on the originating function, secret-masked) and `dep_auditor` (unpinned or
URL-sourced dependencies, custom package indexes and risky `postinstall`-style scripts — fully offline). Project plugins go in `.aether/plugins/*.py`,
subclass `plugins.base.BaseAetherPlugin`, and are loaded only when listed in `trusted_plugins` in
`.aether/config.json` and they pass a static audit (no `eval`/`exec`/`subprocess`/sockets).

## Security notes

* The dashboard binds to `127.0.0.1`, checks the `Host` header, and requires a per-process token for
  every mutating endpoint. Foreign WebSocket origins are rejected.
* The vault that maps `<<AETHER_MASKED_SECRET_N>>` back to values exists only in process memory.
* `.env`, private keys and certificates are never indexed, and they are denied by the default policy.
* `.aether/policy.json` lets you hide paths from agents (`deny`), protect them from every AetherOS write
  (`read_only`) and restrict which commands `heal` may run (`allowed_commands`). An invalid policy file
  fails closed (read-only lockdown).
* `aether install-hooks` blocks commits whose added lines contain secrets or prompt injections; bypass a
  false positive once with `git commit --no-verify` or permanently with `scan_ignore`.
* Detection is heuristic. It's a safety net, not a replacement for a secrets manager or code review.

## Supported languages

Python (native AST: classes, functions, methods, imports, calls, FastAPI/Flask routes with router
prefixes), TypeScript/JavaScript (functions, arrow functions, classes and methods, interfaces, imports,
Express and Next.js routes, `fetch`/`axios` calls), Go, Rust (actix/axum routes), JSON (`package.json`
dependencies and scripts), Dockerfile, Markdown sections, SQL tables, YAML/TOML/HTML/CSS/shell as
module nodes.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md), [ARCHITECTURE.md](ARCHITECTURE.md) and [CLAUDE.md](CLAUDE.md).

```powershell
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

MIT licensed.
