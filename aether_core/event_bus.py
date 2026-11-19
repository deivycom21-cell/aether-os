"""Async in-memory pub/sub with cross-process fan-out through the SQLite ``events`` table.

* In-process subscribers (WebSocket handlers, the TUI) receive events through ``asyncio.Queue``s.
* ``publish`` is safe to call from any thread (MCP tools run in worker threads) and from sync code.
* Every event is also persisted so a dashboard running in *another* process (``aether web``) can
  tail the table and animate what an MCP server launched by Claude Code is doing.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aether_core.database import AetherDB


class EventType:
    CONTEXT_QUERY = "CONTEXT_QUERY"
    SCAR_TRIGGERED = "SCAR_TRIGGERED"
    SCAR_RECORDED = "SCAR_RECORDED"
    SECRET_BLOCKED = "SECRET_BLOCKED"
    INJECTION_DETECTED = "INJECTION_DETECTED"
    SWARM_STEP = "SWARM_STEP"
    SNAPSHOT_CREATED = "SNAPSHOT_CREATED"
    ROLLBACK = "ROLLBACK"
    INDEX_UPDATED = "INDEX_UPDATED"
    IMPACT_ANALYSIS = "IMPACT_ANALYSIS"
    SECURITY_SCAN = "SECURITY_SCAN"
    PLUGIN_RUN = "PLUGIN_RUN"
    BRAIN_SYNC = "BRAIN_SYNC"

    ALL = (
        CONTEXT_QUERY, SCAR_TRIGGERED, SCAR_RECORDED, SECRET_BLOCKED, INJECTION_DETECTED, SWARM_STEP,
        SNAPSHOT_CREATED, ROLLBACK, INDEX_UPDATED, IMPACT_ANALYSIS, SECURITY_SCAN, PLUGIN_RUN, BRAIN_SYNC,
    )


PROCESS_ORIGIN = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


@dataclass(slots=True)
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    nodes: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    origin: str = PROCESS_ORIGIN
    id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "ts": self.ts,
            "origin": self.origin,
            "nodes": list(self.nodes),
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Event:
        return cls(
            type=str(d["type"]),
            payload=dict(d.get("payload") or {}),
            nodes=[str(n) for n in d.get("nodes") or []],
            ts=float(d.get("ts") or time.time()),
            origin=str(d.get("origin") or ""),
            id=int(d.get("id") or 0),
        )


class _Subscriber:
    __slots__ = ("queue", "loop", "types")

    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int, types: frozenset[str] | None) -> None:
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.loop = loop
        self.types = types

    def offer(self, event: Event) -> None:
        if self.types is not None and event.type not in self.types:
            return

        def _put() -> None:
            if self.queue.full():
                try:
                    self.queue.get_nowait()  # drop oldest: telemetry must never block producers
                except asyncio.QueueEmpty:
                    pass
            self.queue.put_nowait(event)

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            _put()
        elif not self.loop.is_closed():
            self.loop.call_soon_threadsafe(_put)


class EventBus:
    def __init__(self, db: AetherDB | None = None, history: int = 500) -> None:
        self.db = db
        self._subs: list[_Subscriber] = []
        self._sync_listeners: list[Callable[[Event], None]] = []
        self._lock = threading.Lock()
        self.history: deque[Event] = deque(maxlen=history)

    # ------------------------------------------------------------------ publish
    def publish(self, type_: str, payload: dict[str, Any] | None = None, nodes: list[str] | None = None) -> Event:
        event = Event(type=type_, payload=payload or {}, nodes=list(nodes or []))
        if self.db is not None:
            try:
                event.id = self.db.insert_event(event.type, event.origin, event.nodes, event.payload, event.ts)
            except Exception:  # noqa: BLE001 - telemetry persistence must never break the caller
                event.id = 0
        self._dispatch(event)
        return event

    def dispatch_external(self, event: Event) -> None:
        """Deliver an event that originated in another process (read from the DB tail)."""
        self._dispatch(event)

    def _dispatch(self, event: Event) -> None:
        self.history.append(event)
        with self._lock:
            subs = list(self._subs)
            listeners = list(self._sync_listeners)
        for sub in subs:
            sub.offer(event)
        for fn in listeners:
            try:
                fn(event)
            except Exception:  # noqa: BLE001
                continue

    # ------------------------------------------------------------------ subscribe
    def add_listener(self, fn: Callable[[Event], None]) -> Callable[[], None]:
        with self._lock:
            self._sync_listeners.append(fn)

        def remove() -> None:
            with self._lock:
                if fn in self._sync_listeners:
                    self._sync_listeners.remove(fn)

        return remove

    def subscribe(self, types: list[str] | None = None, maxsize: int = 1000) -> _Subscriber:
        sub = _Subscriber(asyncio.get_running_loop(), maxsize, frozenset(types) if types else None)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    async def stream(self, types: list[str] | None = None) -> AsyncIterator[Event]:
        sub = self.subscribe(types)
        try:
            while True:
                yield await sub.queue.get()
        finally:
            self.unsubscribe(sub)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


class DBEventTailer:
    """Polls the ``events`` table and forwards events produced by other processes to a bus."""

    def __init__(self, db: AetherDB, bus: EventBus, interval: float = 0.4) -> None:
        self.db = db
        self.bus = bus
        self.interval = interval
        self.last_id = db.max_event_id()
        self._task: asyncio.Task[None] | None = None

    def poll_once(self) -> list[Event]:
        rows = self.db.events_since(self.last_id)
        delivered: list[Event] = []
        for row in rows:
            self.last_id = max(self.last_id, int(row["id"]))
            if row["origin"] == PROCESS_ORIGIN:
                continue  # already dispatched in-process
            ev = Event.from_dict(row)
            self.bus.dispatch_external(ev)
            delivered.append(ev)
        return delivered

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.poll_once)
            except Exception:  # noqa: BLE001 - keep tailing even if the DB is briefly locked
                pass
            await asyncio.sleep(self.interval)

    def start(self) -> asyncio.Task[None]:
        self._task = asyncio.get_running_loop().create_task(self.run())
        return self._task

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
