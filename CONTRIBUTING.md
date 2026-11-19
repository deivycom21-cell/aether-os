# Contributing to AetherOS

Thanks for helping. AetherOS is a local-first tool, so contributions must keep it that way: no hosted
services, no telemetry leaving the machine, and no multi-gigabyte model downloads.

## Setup

```powershell
git clone <your fork> aether-os; cd aether-os
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

On macOS/Linux use `.venv/bin/python` instead. The swarm and git-history tests need `git` on `PATH`;
they are skipped otherwise.

## Ground rules

1. **Tests first-class.** Every behaviour change comes with a test in the matching suite:
   `test_database_and_ast.py`, `test_security_firewall.py`, `test_mcp_tools.py`,
   `test_swarm_and_timetravel.py`, `test_web_and_plugins.py`, and the v0.2 suites
   `test_v02_heal_and_rules.py`, `test_v02_plugins.py`, `test_v02_analysis.py`,
   `test_v02_sessions_policy_budget.py`, `test_v02_tooling.py`.
2. **No credential-shaped literals** anywhere in the repo, including tests. Generate them with
   `tests.conftest.fake_secret()`. GitHub push protection will block you otherwise, and rightly so.
3. **Cross-platform.** `pathlib`, `encoding="utf-8"`, argument-list subprocesses, no shell scripts.
4. **Secrets never leave the vault.** Anything returned to an agent, written to the team brain, or sent
   to the dashboard must pass through `SecretVault.mask` / `mask_obj`.
5. **Typed and complete.** Full type hints, no `TODO`s, no stub functions.

## Adding a language parser

1. Write `parse_<lang>(text, rel_path) -> ParsedFile` in `aether_core/ast_indexer.py`. Emit a `module`
   node plus symbol nodes (`function`, `class`, `method`, `route`, ...) and `imports` / `http_call` edges.
2. Register the extension in `LANG_BY_EXT` and dispatch it in `parse_file`.
3. If imports can be resolved to files, extend `GraphResolver._resolve_import`.
4. Add a parser test with a realistic snippet.

## Writing a plugin

```python
from plugins.base import BaseAetherPlugin

class LicenseAudit(BaseAetherPlugin):
    name = "license_audit"
    description = "Flags dependencies without an approved license."

    def run(self) -> dict:
        deps = self.kernel.db.nodes_by_kind("dependency")
        return {"dependencies": len(deps)}
```

Built-in plugins live in `plugins/`. Project-local plugins live in `.aether/plugins/` and must be
listed in `trusted_plugins` in `.aether/config.json`. They also have to pass the static audit
(`plugins.base.audit_plugin_source`).

## Pull requests

* Keep PRs focused, and describe the user-visible change.
* Run the full suite and `aether benchmark`. Mention it in the PR if savings or latency move noticeably.
* For dashboard changes, check both renderers: WebGL, and the Canvas 2D fallback (block the CDN in
  devtools).
