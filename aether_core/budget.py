"""Context budget profiler: how many tokens each session / MCP tool / file / query pushes into the agent.

Every MCP tool response is measured after secret masking (exactly what the agent receives) and stored
in ``budget_usage``. Crossing 80 %, 100 %, 150 %, 200 % and 300 % of the configured threshold emits a
``BUDGET_WARNING`` event that the dashboard turns into a HUD alert. Crossing detection compares the
session total before and after each record, so it is stateless and works across processes.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from typing import TYPE_CHECKING, Any

from aether_core.context_engine import estimate_tokens
from aether_core.event_bus import PROCESS_ORIGIN

if TYPE_CHECKING:
    from aether_core.database import AetherDB
    from aether_core.event_bus import EventBus

DEFAULT_BUDGET_TOKENS = 60_000
WARNING_LEVELS = (0.8, 1.0, 1.5, 2.0, 3.0)
_FILE_KEYS = ("file", "file_path", "path")


def default_session_id() -> str:
    explicit = os.environ.get("AETHER_SESSION", "").strip()
    if explicit:
        return explicit[:80]
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{PROCESS_ORIGIN.rsplit(':', 1)[-1]}"


def files_in(obj: Any, limit: int = 40) -> list[str]:
    """Collect file paths mentioned in a tool response (``file``/``file_path``/``path`` keys)."""
    found: list[str] = []

    def walk(cur: Any, depth: int) -> None:
        if len(found) >= limit or depth > 12:
            return
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k in _FILE_KEYS and isinstance(v, str) and v and len(v) < 400 and "\n" not in v:
                    found.append(v)
                elif isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(cur, list):
            for item in cur:
                walk(item, depth + 1)

    walk(obj, 0)  # document order: the most relevant results come first
    return list(dict.fromkeys(found))[:limit]


class BudgetTracker:
    def __init__(self, db: AetherDB, bus: EventBus, threshold: int = DEFAULT_BUDGET_TOKENS, session: str | None = None) -> None:
        self.db = db
        self.bus = bus
        self.threshold = max(1, int(threshold))
        self.session = session or default_session_id()

    def measure(self, obj: Any) -> int:
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
        return estimate_tokens(text)

    def record(self, tool: str, response: Any, query: str = "", files: list[str] | None = None,
               tokens: int | None = None) -> dict[str, Any]:
        """Account one tool response. Never raises: budget telemetry must not break the caller."""
        try:
            used_now = self.measure(response) if tokens is None else int(tokens)
            paths = files if files is not None else files_in(response)
            before = self.db.budget_session_total(self.session)
            self.db.record_budget(self.session, tool, used_now, {"files": paths[:40], "query": query[:200]})
            after = before + used_now
            crossed = [lvl for lvl in WARNING_LEVELS if before < lvl * self.threshold <= after]
            status = self._status(after)
            if crossed:
                level = crossed[-1]
                self.bus.publish("BUDGET_WARNING", {
                    "session": self.session, "tool": tool, "used": after, "threshold": self.threshold,
                    "pct": status["pct"], "level": "exceeded" if level >= 1.0 else "approaching",
                    "multiple": level,
                })
                status["warning"] = True
            return status
        except Exception:  # noqa: BLE001 - telemetry never breaks the caller
            return {"session": self.session, "used": 0, "threshold": self.threshold, "pct": 0.0, "warning": False}

    def _status(self, used: int) -> dict[str, Any]:
        pct = round(used / self.threshold * 100.0, 1)
        return {"session": self.session, "used": used, "threshold": self.threshold, "pct": pct,
                "remaining": max(0, self.threshold - used), "warning": False,
                "level": "exceeded" if pct >= 100 else "approaching" if pct >= 80 else "ok"}

    def status(self, session: str | None = None) -> dict[str, Any]:
        """Status of ``session``; defaults to this process' session, else the most recently active one."""
        target = session
        if target is None:
            target = self.session if self.db.budget_session_total(self.session) else None
        if target is None:
            latest = self.db.budget_sessions(1)
            target = latest[0]["session"] if latest else self.session
        st = self._status(self.db.budget_session_total(target))
        st["session"] = target
        return st

    def report(self, session: str | None = None, all_sessions: bool = False, top: int = 10) -> dict[str, Any]:
        sessions = self.db.budget_sessions(20)
        target = None if all_sessions else (session or self.status()["session"])
        details = self.db.budget_details(target)
        per_file: Counter[str] = Counter()
        per_query: Counter[str] = Counter()
        for d in details:
            files = d["detail"].get("files") or []
            if files:
                share = d["tokens"] / len(files)
                for f in files:
                    per_file[f] += share
            q = d["detail"].get("query") or ""
            if q:
                per_query[q] += d["tokens"]
        used = sum(d["tokens"] for d in details)
        return {
            "session": target or "ALL",
            "threshold": self.threshold,
            "used": used,
            "pct": round(used / self.threshold * 100.0, 1) if target else None,
            "calls": len(details),
            "by_tool": self.db.budget_by_tool(target),
            "top_files": [{"file": f, "tokens": int(round(t))} for f, t in per_file.most_common(top)],
            "top_queries": [{"query": q, "tokens": t} for q, t in per_query.most_common(top)],
            "heaviest_context_queries": self.db.telemetry_top_queries(top),
            "sessions": sessions,
        }
