# AetherOS architecture

## 1. The big picture

```
 ┌────────────────────────┐   stdio (MCP)   ┌──────────────────────────────────────────────────────┐
 │  Claude Code / Cursor  │ ◀─────────────▶ │  FastMCP server  (aether_core/mcp_server.py)         │
 │  Windsurf / Aider(CLI) │                 │  query_smart_context · impact_analysis · record_scar │
 │  Cline/Roo · Zed ·     │                 │  check_scars · create/rollback_snapshot · scan ·     │
 │  OpenCode (v0.2)       │                 │  get_telemetry_stats · v0.2: prepare_heal_context ·  │
 └────────────────────────┘                 │  get_code_hotspots · semantic_diff · find_dead_code ·│
                                            │  save_session_checkpoint · resume_session ·          │
                                            │  check_path_policy · get_context_budget              │
                                            │  every output → mask → budget meter (budget_usage)   │
                                            └───────────────┬──────────────────────────────────────┘
                                                            │ every output → SecretVault.mask_obj
                                                            ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
 │                                  AetherKernel  (aether_core/kernel.py)                            │
 │  ┌──────────────┐  ┌─────────────────┐  ┌──────────────┐  ┌───────────────┐  ┌────────────────┐  │
 │  │ ast_indexer  │  │ context_engine  │  │  security    │  │   git_sync    │  │ aether_swarm   │  │
 │  │ py/ts/js/go/ │  │ hybrid search + │  │ regex+entropy│  │ team brain +  │  │ router + work- │  │
 │  │ rs/json/dock │  │ compression +   │  │ vault, prompt│  │ snapshots     │  │ trees + verify │  │
 │  │ + resolver   │  │ savings (USD)   │  │ injection    │  │ (time travel) │  │ + merge        │  │
 │  └──────┬───────┘  └───────┬─────────┘  └──────┬───────┘  └──────┬────────┘  └───────┬────────┘  │
 │         └──────────────────┴───────────┬───────┴─────────────────┴───────────────────┘           │
 │                                        ▼                                                          │
 │   ┌──────────────────────────────────────────────┐        ┌────────────────────────────────────┐ │
 │   │  SQLite brain (WAL)  .aether/brain.db         │◀──────▶│  Event bus (async pub/sub in RAM)  │ │
 │   │  repos·files·nodes·edges·nodes_fts(FTS5)·     │ persist│  publish() from any thread         │ │
 │   │  scars·decisions·telemetry·events·snapshots   │ events │  asyncio.Queue per subscriber      │ │
 │   └──────────────────────────────────────────────┘        └───────────────┬────────────────────┘ │
 └───────────────────────────────────────────────────────────────────────────┼──────────────────────┘
                                                                             │
                              ┌──────────────────────────────────────────────┼─────────────────────┐
                              ▼                                              ▼                     ▼
               ┌────────────────────────────┐              ┌──────────────────────────┐  ┌──────────────────┐
               │ FastAPI  (aether_web)       │  WebSocket   │ 3D digital twin           │  │ Textual TUI       │
               │ REST /api/* + DBEventTailer │ ───────────▶ │ Three.js / Canvas2D       │  │ (aether_tui)      │
               │ /ws/telemetry               │ /ws/telemetry│ pulses between nodes      │  │ tails events table│
               └────────────────────────────┘              └──────────────────────────┘  └──────────────────┘
```

## 2. Cross-process telemetry

Claude Code launches `aether mcp` as **its own process**; the dashboard (`aether web`) and the TUI are
other processes. They share nothing but the SQLite file, so the event bus persists every event:

```
 MCP process                          SQLite (WAL)                        Web process
 ───────────                          ────────────                        ───────────
 bus.publish(CONTEXT_QUERY) ──insert──▶ events(id, origin=pid-A, …)
                                                        ◀──poll 0.4s── DBEventTailer (skips origin=pid-B)
                                                                         │ bus.dispatch_external(ev)
                                                                         ▼
                                                                  asyncio.Queue → /ws/telemetry → browser
```

WAL mode lets the MCP server write while the dashboard reads without blocking.

## 3. Indexing pipeline

```
 iter_source_files(root)          ── .gitignore/.aetherignore + default ignores, never .env/keys
        │ sha1 unchanged? ──yes──▶ skip (incremental)
        ▼
 parse_file(text, rel, lang)      ── Python: ast (statement walk) · TS/JS/Go/Rust: regex + brace matching
        │                            JSON/Dockerfile/Markdown/SQL/YAML/TOML: structural nodes
        │                            ≥64 files → ProcessPoolExecutor
        ▼
 db.replace_file()                ── one transaction: nodes + edges + FTS rows
        ▼
 GraphResolver.resolve()          ── imports → files (py modules, TS paths+ext inference, Go dirs, Rust mods)
        │                            calls/extends → symbols via the import graph
        │                            http_call "POST /api/payments" → route node in ANY linked repo
        ▼
 plugins (docker, git scars, env) ── add services, scars, env var names
        ▼
 meta.index_version++             ── invalidates the in-memory TF-IDF vector matrix
```

## 4. Context compression (`query_smart_context`)

```
 query ─┬─▶ FTS5 BM25 (weighted columns) ─┐
        └─▶ hashed TF-IDF cosine (numpy) ─┴─▶ Reciprocal Rank Fusion ─▶ kind/name boosts
                                                         │
     relevance cutoff (35% of top) · skip fragments nested in already-sent ones · token budget
                                                         ▼
  package = { results[signature, doc, exact snippet window], relations[deps, dependents,
              cross-repo consumers], scars, warnings }  ─▶ prompt-injection check ─▶ SecretVault
                                                         ▼
  metrics: raw_tokens (full files an agent would read) vs compressed_tokens → USD (Sonnet/Opus)
```

## 5. Security shield

```
 any text ─▶ 13 regex rules (AWS, Anthropic, OpenAI, GitHub, Stripe, Slack, Google, JWT, PEM keys,
             connection strings, generic assignments) ─▶ Shannon entropy > 4.5 on 20+ char tokens
             ─▶ placeholder filter ─▶ <<AETHER_MASKED_SECRET_N>> (same secret ⇒ same token, RAM only)

 external content ─▶ PromptInjectionDetector: override/exfiltration/role-hijack/chat-template tokens,
                     comment-hidden instructions (×1.25), bidi/zero-width unicode, base64 payloads
                     ─▶ score → severity → INJECTION_DETECTED event, snippet withheld
```

## 6. Time travel

```
 create(label): tracked files (git ls-files ∪ untracked-not-ignored) ─▶ sha256 blobs in
                .aether/snapshots/_objects (dedup) + manifest.json
 rollback(id):  diff ─▶ safety snapshot ─▶ atomic restore (tmp + os.replace) ─▶ prune new files ─▶ re-index
```

## 7. Swarm

```
 aether swarm "feature"
   ├─ plan: smart context + ComplexityRouter per role (LOCAL_OLLAMA | FAST | BALANCED | ARCHITECT)
   ├─ git worktree add -b aether/<run>/<role>  .aether/worktrees/<run>/<role>   (architect, builder,
   │                                                                             tester, security_auditor)
   ├─ briefs/<role>.md (mission, relevant files, scars, routing)
   ├─ [--execute, BYOK] architect PLAN.md → builder <<<FILE>>> blocks → tester tests (secret-checked)
   ├─ swarm-verify: integration worktree, --no-ff merges, changed-file secret/injection audit, test command
   └─ swarm-merge: clean-tree check → snapshot → --no-ff merge → re-index;  swarm-cleanup
```

## 8. Web security model

* Binds `127.0.0.1`; Host header allowlist (anti DNS-rebinding).
* Mutating endpoints require `X-Aether-Token` (random per process, injected into the served page).
* WebSocket rejects foreign `Origin`s. All dynamic DOM uses `textContent` (no HTML injection from repo data).

## 9. v0.2.0 subsystems

All new features are kernel methods (`AetherKernel.*`) backed by focused modules; the CLI, MCP server and
web API only call the kernel. Nothing new talks to the network.

```
 aether_core/
   policy.py        .aether/policy.json  deny · read_only · allowed_commands · scan_ignore (mtime-reloaded)
   healer.py        run cmd → parse_failure → frames+symbols → scars → smart context → last_heal_context.json
                    --execute: snapshot → ≤3 × {LLM file blocks → secret check → policy check → write → re-run}
   rules_sync.py    <!-- AETHER:START --> … <!-- AETHER:END --> upsert (CRLF-preserving, atomic replace)
   share_card.py    1200×630 SVG, system fonts only
   mcp_install.py   claude · cursor · windsurf · vscode(cline/roo) · zed(JSONC) · opencode
   hotspots.py      risk = 100·(0.30·cx + 0.25·fan_in + 0.25·churn + 0.20·scars), each log-normalised
   sessions.py      checkpoint builder (progressive shrinking until ≤ 400 tokens)
   precommit.py     git diff --cached -U0 → added lines only → detect_secrets + injection detector
   hooks.py         sh hook → base CPython -S + PYTHONPATH (no venv launcher/site) → chains pre-commit.local
   semantic_diff.py ref vs working tree via parse_file(): added/removed/renamed(shape hash)/signature/body
   dead_code.py     no resolved edge ∧ identifier appears only at its definition(s); route text references
   test_gen.py      pytest (signature locks, smoke calls, route probes) · vitest (arity, smoke)
   docs_gen.py      module graph (dir-level aggregation of resolved edges) → Mermaid + endpoint table
   budget.py        BudgetTracker.record() after masking; stateless threshold-crossing detection
 plugins/ db_schema_inspector · runtime_log_watcher · dep_auditor
```

### Policy enforcement points

```
 read  (deny)       ContextEngine.query (results + relations, one snapshot per query, memoised)
                    impact_analysis · /api/file (403) · /api/graph · hotspots · dead code · semantic diff
                    heal frames · rules facts · checkpoints
 write (read_only)  rollback_snapshot (refuses before the safety snapshot) · heal --execute · gen-tests
                    docs · sync-rules
 exec               heal / prepare_heal_context: argv prefix must match allowed_commands
 commit             precommit.scan_staged skips scan_ignore globs
```

### Plugin-owned virtual records

`db_schema_inspector` stores entities under synthetic paths `@aether/db_schema/<source>`. The indexer never
deletes paths with the `@aether/` prefix (`ast_indexer.is_virtual_path`); the plugin prunes its own stale
records. Hotspots, dead code and docs skip virtual paths.

### Context budget

```
 MCP tool → kernel → SecretVault.mask_obj → estimate_tokens(json) → budget_usage(session, tool, tokens, files, query)
                                                          │ before < L·threshold ≤ after, L ∈ {0.8, 1, 1.5, 2, 3}
                                                          ▼
                                             BUDGET_WARNING → events table → DBEventTailer → /ws/telemetry → HUD alert
```

The session id is `AETHER_SESSION` or `<timestamp>-<pid tag>`; the dashboard shows the most recently active
session. Threshold: `budget_tokens` in `.aether/config.json` (`aether budget --threshold N`).

### Schema

`schema_version` stays `1` (format contract); `schema_revision = 2` records the additive v0.2 tables
`session_checkpoints` and `budget_usage`. Older brains upgrade in place on open.

