"""Universal MCP installer: registers the Aether stdio server in every major agentic IDE's project config.

| target    | file                   | shape                                                            |
|-----------|------------------------|------------------------------------------------------------------|
| claude    | ``.mcp.json``          | ``{"mcpServers": {"aether": {"command", "args"}}}``             |
| cursor    | ``.cursor/mcp.json``   | same as Claude Code                                              |
| windsurf  | ``.windsurf/mcp.json`` | same as Claude Code                                              |
| vscode    | ``.vscode/mcp.json``   | ``{"servers": {"aether": {"type": "stdio", "command", "args"}}}`` (Cline / Roo Code / Copilot) |
| zed       | ``.zed/settings.json`` | ``{"context_servers": {"aether": {"source": "custom", ...}}}`` (JSONC tolerated) |
| opencode  | ``opencode.json``      | ``{"mcp": {"aether": {"type": "local", "command": [...]}}}``    |

Existing files are merged (other servers/settings are preserved); invalid files are never overwritten.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SERVER_NAME = "aether"


@dataclass(frozen=True, slots=True)
class Target:
    key: str
    label: str
    rel_path: str


TARGETS: dict[str, Target] = {
    "claude": Target("claude", "Claude Code", ".mcp.json"),
    "cursor": Target("cursor", "Cursor", ".cursor/mcp.json"),
    "windsurf": Target("windsurf", "Windsurf", ".windsurf/mcp.json"),
    "vscode": Target("vscode", "Cline / Roo Code (VS Code)", ".vscode/mcp.json"),
    "zed": Target("zed", "Zed", ".zed/settings.json"),
    "opencode": Target("opencode", "OpenCode", "opencode.json"),
}
ALIASES = {"claude-code": "claude", "cline": "vscode", "roo": "vscode", "roocode": "vscode", "roo-code": "vscode",
           "vs-code": "vscode", "code": "vscode"}
DEFAULT_TARGETS = ("claude", "cursor")


def resolve_targets(names: list[str] | None, install_all: bool = False) -> list[Target]:
    if install_all or (names and any(n.lower() == "all" for n in names)):
        return list(TARGETS.values())
    keys = [ALIASES.get(n.lower(), n.lower()) for n in (names or DEFAULT_TARGETS)]
    unknown = [k for k in keys if k not in TARGETS]
    if unknown:
        raise ValueError(f"unknown target(s): {', '.join(unknown)}. Valid: {', '.join([*TARGETS, *ALIASES])}, all")
    return [TARGETS[k] for k in dict.fromkeys(keys)]


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments plus trailing commas outside strings (Zed/VS Code settings are JSONC)."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(ch)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8-sig")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(strip_jsonc(raw))
    if not isinstance(data, dict):
        raise ValueError("top-level value is not a JSON object")
    return data


def merge_entry(target: Target, data: dict[str, Any], command: str, args: list[str]) -> dict[str, Any]:
    if target.key == "vscode":
        data.setdefault("servers", {})[SERVER_NAME] = {"type": "stdio", "command": command, "args": args}
    elif target.key == "zed":
        data.setdefault("context_servers", {})[SERVER_NAME] = {"source": "custom", "command": command, "args": args, "env": {}}
    elif target.key == "opencode":
        data.setdefault("$schema", "https://opencode.ai/config.json")
        data.setdefault("mcp", {})[SERVER_NAME] = {"type": "local", "command": [command, *args], "enabled": True}
    else:
        data.setdefault("mcpServers", {})[SERVER_NAME] = {"command": command, "args": args}
    return data


def install(project: Path, targets: list[Target], command: str, args: list[str], dry_run: bool = False) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for t in targets:
        path = project / Path(t.rel_path)
        try:
            data = load_config(path)
        except (ValueError, json.JSONDecodeError) as exc:
            results.append({"target": t.key, "label": t.label, "path": str(path), "ok": False,
                            "error": f"not valid JSON, left untouched ({exc})"})
            continue
        existed = path.is_file()
        merged = merge_entry(t, data, command, args)
        text = json.dumps(merged, indent=2) + "\n"
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".aether-tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        results.append({"target": t.key, "label": t.label, "path": str(path), "ok": True,
                        "action": "would write" if dry_run else ("updated" if existed else "created"), "content": merged})
    return results
