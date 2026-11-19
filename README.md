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
224,297 -> 21,000 tokens (-90.6%) across 15 real queries on this repo · p50 33 ms
```

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
.\.venv\Scripts\aether.exe install-mcp                # writes .mcp.json (Claude Code) + .cursor/mcp.json
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
| `get_telemetry_stats()` | Tokens and USD saved, graph size, security counters, swarm state |

Resources: `aether://telemetry`, `aether://scars`, `aether://graph/summary`, `aether://decisions`.

## CLI

```
aether init [--link-repo PATH]...     aether index [--force]          aether link/unlink/repos
aether query "..." [-t 1500] [--json] aether impact FILE              aether scan [PATH]
aether scar FILE "error" --fix "..."  aether scars [FILE]             aether decision TITLE BODY
aether snapshot [LABEL]               aether snapshots / diff ID      aether rollback ID [-y]
aether swarm "feature" [--execute]    aether swarm-status/-execute/-verify/-merge/-cleanup ID
aether route "prompt"                 aether export-brain / import-brain
aether web [--open]   aether tui      aether watch    aether mcp      aether plugins   aether benchmark
```

## The 3D digital twin

`aether web` serves a self-contained page (Three.js from a CDN, with an automatic Canvas 2D fallback
when you're offline or WebGL is unavailable). Files are nodes clustered by repository and directory,
dependencies are edges, and every kernel event — `CONTEXT_QUERY`, `SCAR_TRIGGERED`, `SECRET_BLOCKED`,
`SWARM_STEP`, `SNAPSHOT_CREATED` — sends light pulses from the core to the files involved, live over
`/ws/telemetry`. Events are shared through SQLite, so the dashboard animates what the MCP server
inside Claude Code is doing in another process. Hover a node for details, click it for symbols,
dependencies, scars and impact analysis, and roll back snapshots from the side panel.

`aether tui` is the terminal version: KPIs, savings sparkline, live event bus, scars and snapshots,
with `i` (re-index), `s` (snapshot) and `x` (scan) key bindings.

## Benchmark (this repository)

`aether benchmark` (or `python benchmarks/run_benchmark.py`) indexes the repo into a throwaway
database and runs 15 realistic queries. Results on a Windows 11 laptop, Python 3.12:

| Metric | Value |
|---|---|
| Graph | 36 files · 610 symbols · 2,915 edges |
| Cold index / warm re-index | 876 ms / 70 ms |
| Query latency | p50 33 ms · p95 48 ms (first query 455 ms builds the vector matrix) |
| Tokens (raw files of the hits → AetherOS package) | 224,297 → 21,000 (**90.6% saved**) |
| Input cost saved over the 15 queries | $0.41 on Sonnet 5.5 · $0.81 on Opus 5.5 |

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
un-ignored env files; only variable **names** are stored). Project plugins go in `.aether/plugins/*.py`,
subclass `plugins.base.BaseAetherPlugin`, and are loaded only when listed in `trusted_plugins` in
`.aether/config.json` and they pass a static audit (no `eval`/`exec`/`subprocess`/sockets).

## Security notes

* The dashboard binds to `127.0.0.1`, checks the `Host` header, and requires a per-process token for
  every mutating endpoint. Foreign WebSocket origins are rejected.
* The vault that maps `<<AETHER_MASKED_SECRET_N>>` back to values exists only in process memory.
* `.env`, private keys and certificates are never indexed.
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
