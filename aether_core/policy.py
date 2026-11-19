"""Granular permission sandbox (``.aether/policy.json``).

* ``deny``        – globs never exposed to agents (query results, impact analysis, file detail) and never written.
* ``read_only``   – globs agents may read but AetherOS will never modify (heal, rollback, gen-tests, docs, sync-rules).
* ``allowed_commands`` – command prefixes AetherOS may execute on an agent's behalf (``heal``,
  ``prepare_heal_context``). ``"*"`` allows everything.
* ``scan_ignore`` – globs skipped by the pre-commit secret/injection guard (e.g. security test fixtures).

Without a policy file the defaults below apply: no extra restrictions on source files, secret material
denied, and only well-known test/build runners allowed.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

POLICY_FILENAME = "policy.json"

DEFAULT_DENY = [
    "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", ".env", ".env.local",
    ".env.development", ".env.production", ".env.test",
]
DEFAULT_ALLOWED_COMMANDS = [
    "pytest", "python -m pytest", "py -m pytest", "python -m unittest", "npm test", "npm run test",
    "npm run build", "npm run lint", "npm run typecheck", "pnpm test", "pnpm run test", "pnpm run build",
    "yarn test", "yarn run test", "yarn build", "npx vitest", "npx jest", "npx tsc", "vitest", "jest", "tsc",
    "bun test", "deno test", "go test", "go build", "go vet", "cargo test", "cargo build", "cargo check",
    "ruff", "ruff check", "mypy", "python -m mypy", "node --check", "make test", "dotnet test", "dotnet build",
    "mvn test", "gradle test",
]
_EXEC_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".com")
_PYTHON_NAMES = {"python", "python3", "py", "pythonw"}


def normalize_policy_path(path: str) -> str:
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def glob_match(path: str, pattern: str) -> bool:
    """gitignore-flavoured matching: ``dir/`` and ``dir/**`` cover the subtree, bare names match basenames."""
    rel = normalize_policy_path(path)
    pat = normalize_policy_path(pattern)
    if not pat:
        return False
    if pat.endswith("/**"):
        pat = pat[:-3]
        return rel == pat or rel.startswith(pat + "/")
    if pattern.strip().endswith("/"):
        pat = pat.rstrip("/")
        return rel == pat or rel.startswith(pat + "/")
    if "/" not in pat:
        name = rel.rsplit("/", 1)[-1]
        return fnmatch.fnmatchcase(name, pat) or fnmatch.fnmatchcase(rel, pat)
    return fnmatch.fnmatchcase(rel, pat) or rel.startswith(pat + "/")


def command_tokens(argv: list[str]) -> list[str]:
    """Normalise an argument vector for prefix matching (``C:\\..\\python.exe`` -> ``python``)."""
    if not argv:
        return []
    head = PurePosixPath(argv[0].replace("\\", "/")).name.lower()
    for suffix in _EXEC_SUFFIXES:
        if head.endswith(suffix):
            head = head[: -len(suffix)]
            break
    if head.startswith("python") and head[6:].replace(".", "").isdigit():
        head = "python"
    if Path(argv[0]).resolve() == Path(sys.executable).resolve() or head in _PYTHON_NAMES:
        head = "python"
    return [head, *argv[1:]]


@dataclass(slots=True)
class Policy:
    read_only: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=lambda: list(DEFAULT_DENY))
    allowed_commands: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_COMMANDS))
    scan_ignore: list[str] = field(default_factory=list)
    source: str = "default"
    _decisions: dict[tuple[str, str], str | None] = field(default_factory=dict, repr=False, compare=False)

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, aether_dir: Path) -> Policy:
        path = aether_dir / POLICY_FILENAME
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A broken policy must fail closed for writes/commands but keep secrets denied.
            return cls(read_only=["**"], allowed_commands=[], source=f"{path} (invalid JSON: read-only lockdown)")
        if not isinstance(data, dict):
            return cls(read_only=["**"], allowed_commands=[], source=f"{path} (invalid: read-only lockdown)")

        def strings(key: str, default: list[str]) -> list[str]:
            value = data.get(key, default)
            return [str(v) for v in value] if isinstance(value, list) else list(default)

        return cls(
            read_only=strings("read_only", []),
            deny=strings("deny", DEFAULT_DENY),
            allowed_commands=strings("allowed_commands", DEFAULT_ALLOWED_COMMANDS),
            scan_ignore=strings("scan_ignore", []),
            source=str(path),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"read_only": list(self.read_only), "deny": list(self.deny),
                "allowed_commands": list(self.allowed_commands), "scan_ignore": list(self.scan_ignore)}

    def save(self, aether_dir: Path) -> Path:
        aether_dir.mkdir(parents=True, exist_ok=True)
        path = aether_dir / POLICY_FILENAME
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        self.source = str(path)
        self._decisions.clear()  # lists may have been edited in place before saving
        return path

    # ------------------------------------------------------------------ checks
    def _matches(self, path: str, patterns: list[str], bucket: str = "") -> str | None:
        key = (bucket, path)
        if bucket and key in self._decisions:
            return self._decisions[key]
        found = None
        for pat in patterns:
            if pat.strip() in {"**", "*"} or glob_match(path, pat):
                found = pat
                break
        if bucket and len(self._decisions) < 50_000:
            self._decisions[key] = found
        return found

    def is_denied(self, path: str) -> bool:
        return self._matches(path, self.deny, "deny") is not None

    def is_read_only(self, path: str) -> bool:
        return self._matches(path, self.read_only, "read_only") is not None

    def can_read(self, path: str) -> bool:
        return not self.is_denied(path)

    def can_write(self, path: str) -> bool:
        return not self.is_denied(path) and not self.is_read_only(path)

    def scan_ignored(self, path: str) -> bool:
        return self._matches(path, self.scan_ignore) is not None

    def explain(self, path: str) -> dict[str, Any]:
        deny = self._matches(path, self.deny, "deny")
        ro = self._matches(path, self.read_only, "read_only")
        return {
            "path": normalize_policy_path(path),
            "read": deny is None,
            "write": deny is None and ro is None,
            "matched_deny": deny,
            "matched_read_only": ro,
            "scan_ignored": self.scan_ignored(path),
            "policy": self.source,
        }

    def command_allowed(self, argv: list[str]) -> bool:
        if "*" in self.allowed_commands:
            return True
        tokens = command_tokens(argv)
        if not tokens:
            return False
        for allowed in self.allowed_commands:
            want = allowed.split()
            if want and [t.lower() for t in tokens[: len(want)]] == [w.lower() for w in want]:
                return True
        return False
