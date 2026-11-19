"""Cyberpunk/htop-style terminal dashboard with live metrics.

It tails the shared SQLite ``events`` table, so it animates activity produced by any other Aether
process (the MCP server launched by Claude Code, ``aether web``, CLI commands...).
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, RichLog, Sparkline, Static

from aether_core.kernel import AetherKernel

EVENT_STYLES = {
    "CONTEXT_QUERY": "bold #00f0ff",
    "SCAR_TRIGGERED": "bold #f59e0b",
    "SCAR_RECORDED": "#fbbf24",
    "SECRET_BLOCKED": "bold #10b981",
    "INJECTION_DETECTED": "bold #f43f5e",
    "SWARM_STEP": "bold #8b5cf6",
    "SNAPSHOT_CREATED": "#e0f2fe",
    "ROLLBACK": "bold #f59e0b",
    "INDEX_UPDATED": "#6366f1",
    "IMPACT_ANALYSIS": "#a78bfa",
    "SECURITY_SCAN": "#34d399",
    "PLUGIN_RUN": "#64748b",
    "BRAIN_SYNC": "#38bdf8",
}
SEVERITY_STYLES = {"critical": "bold #f43f5e", "high": "#f59e0b", "medium": "#eab308", "low": "#64748b", "info": "#64748b"}


def fmt_num(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    return f"{int(n):,}"


def describe_event(ev: dict[str, Any]) -> str:
    p = ev.get("payload") or {}
    t = ev["type"]
    if t == "CONTEXT_QUERY":
        return f"\"{p.get('query', '')}\" -> {p.get('results', 0)} hits, saved {fmt_num(p.get('saved_tokens', 0))} tok"
    if t == "SCAR_TRIGGERED":
        return f"{p.get('count', 0)} scar(s) on {', '.join(p.get('files', []))}"
    if t == "SCAR_RECORDED":
        return f"new scar on {p.get('file')} [{p.get('severity')}]"
    if t == "SECRET_BLOCKED":
        return f"{p.get('count', 0)} secret(s) masked ({', '.join(p.get('rules', []) or [p.get('context', '')])})"
    if t == "INJECTION_DETECTED":
        return f"prompt injection in {', '.join(p.get('files', [])) or str(p.get('count', '?')) + ' file(s)'}"
    if t == "SWARM_STEP":
        return f"{p.get('run_id')} | {p.get('role')} -> {p.get('step')}"
    if t == "SNAPSHOT_CREATED":
        return f"snapshot {p.get('id')} \"{p.get('label')}\" ({p.get('file_count')} files)"
    if t == "ROLLBACK":
        return f"rollback to {p.get('snapshot_id')}: {p.get('restored')} restored"
    if t == "INDEX_UPDATED":
        return f"{p.get('changed')} file(s) re-indexed" if p.get("incremental") else f"{p.get('files')} files / {p.get('nodes')} symbols indexed"
    if t == "IMPACT_ANALYSIS":
        return f"impact {p.get('file')}: {p.get('risk_level')} ({p.get('dependents')} dependents)"
    if t == "SECURITY_SCAN":
        return f"scan {p.get('risk_level')}: {p.get('secrets')} secrets, {p.get('injections')} injections"
    return ", ".join(f"{k}={v}" for k, v in list(p.items())[:4])


class Kpi(Static):
    def __init__(self, label: str, color: str, id: str) -> None:  # noqa: A002 - textual API name
        super().__init__(id=id, classes="kpi")
        self.label = label
        self.color = color
        self.value = "0"

    def set_value(self, value: str) -> None:
        if value != self.value:
            self.value = value
            self.update(self._render_text())

    def _render_text(self) -> Text:
        t = Text()
        t.append(self.label.upper() + "\n", style="#6b7a90")
        t.append(self.value, style=f"bold {self.color}")
        return t

    def on_mount(self) -> None:
        self.update(self._render_text())


class AetherTUI(App[None]):
    TITLE = "AetherOS"
    CSS = """
    Screen { background: #05070d; color: #d8e3f0; }
    #banner { height: 3; padding: 0 2; background: #070b14; border-bottom: heavy #00f0ff 40%; content-align: left middle; }
    #kpis { height: 5; padding: 0 1; }
    .kpi { width: 1fr; height: 4; margin: 0 1; padding: 0 1; background: #0a0f1c; border: round #00f0ff 30%; }
    #middle { height: 1fr; }
    #left { width: 46%; }
    #spark-box { height: 7; margin: 0 1; border: round #8b5cf6 50%; background: #0a0f1c; padding: 0 1; }
    #spark-title, #scars-title, #feed-title, #snaps-title { height: 1; color: #00f0ff; text-style: bold; }
    Sparkline { height: 4; margin-top: 1; }
    Sparkline > .sparkline--max-color { color: #00f0ff; }
    Sparkline > .sparkline--min-color { color: #8b5cf6; }
    #scars-box { height: 1fr; margin: 0 1; border: round #f59e0b 40%; background: #0a0f1c; padding: 0 1; }
    #feed-box { width: 54%; margin: 0 1; border: round #00f0ff 40%; background: #0a0f1c; padding: 0 1; }
    RichLog { background: #0a0f1c; scrollbar-color: #00f0ff 30%; }
    #snaps-box { height: 9; margin: 0 1; border: round #10b981 40%; background: #0a0f1c; padding: 0 1; }
    DataTable { background: #0a0f1c; }
    DataTable > .datatable--header { background: #0e1526; color: #6b7a90; }
    DataTable > .datatable--cursor { background: #00f0ff 20%; }
    Footer { background: #070b14; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("i", "reindex", "Re-index"),
        Binding("s", "snapshot", "Snapshot"),
        Binding("x", "scan", "Security scan"),
        Binding("c", "clear_feed", "Clear feed"),
    ]

    def __init__(self, root: Path | str | None = None, kernel: AetherKernel | None = None, poll_interval: float = 0.6) -> None:
        super().__init__()
        self.kernel = kernel or AetherKernel(root)
        self.poll_interval = poll_interval
        self.last_event_id = max(0, self.kernel.db.max_event_id() - 30)
        self.events_seen = 0
        self.busy = False

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Static(id="banner")
        with Horizontal(id="kpis"):
            yield Kpi("tokens saved", "#00f0ff", "k-tokens")
            yield Kpi("usd saved", "#10b981", "k-usd")
            yield Kpi("compression", "#8b5cf6", "k-ratio")
            yield Kpi("files / symbols", "#d8e3f0", "k-graph")
            yield Kpi("scars", "#f59e0b", "k-scars")
            yield Kpi("secrets blocked", "#10b981", "k-secrets")
            yield Kpi("swarm", "#8b5cf6", "k-swarm")
        with Horizontal(id="middle"):
            with Vertical(id="left"):
                with Vertical(id="spark-box"):
                    yield Static("TOKENS SAVED PER QUERY", id="spark-title")
                    yield Sparkline([0], summary_function=max, id="spark")
                with Vertical(id="scars-box"):
                    yield Static("SCARS — HOTTEST FILES", id="scars-title")
                    yield DataTable(id="scars", zebra_stripes=True, cursor_type="row")
            with Vertical(id="feed-box"):
                yield Static("LIVE EVENT BUS", id="feed-title")
                yield RichLog(id="feed", markup=False, wrap=True, max_lines=500)
        with Vertical(id="snaps-box"):
            yield Static("TIME TRAVEL SNAPSHOTS", id="snaps-title")
            yield DataTable(id="snaps", zebra_stripes=True, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        scars = self.query_one("#scars", DataTable)
        scars.add_columns("SEV", "FILE", "ERROR", "HITS", "SRC")
        snaps = self.query_one("#snaps", DataTable)
        snaps.add_columns("ID", "LABEL", "FILES", "SIZE", "WHEN")
        self.refresh_all()
        self.set_interval(self.poll_interval, self.poll_events)
        self.set_interval(2.0, self.refresh_all)
        self.set_interval(1.0, self.update_banner)

    # ------------------------------------------------------------------ refresh
    def update_banner(self) -> None:
        s = Text()
        s.append(" AETHER", style="bold #00f0ff")
        s.append("//", style="bold #8b5cf6")
        s.append("OS ", style="bold #00f0ff")
        s.append(f"  ◈ {self.kernel.repo_name}", style="#d8e3f0")
        links = self.kernel.config.linked_repos
        if links:
            s.append(f"  ⇄ {', '.join(r['name'] for r in links)}", style="#8b5cf6")
        s.append(f"   {datetime.now().strftime('%H:%M:%S')}", style="#6b7a90")
        s.append(f"   events {self.events_seen}", style="#6b7a90")
        if self.busy:
            s.append("   ◉ working…", style="bold #f59e0b")
        self.query_one("#banner", Static).update(s)

    def refresh_all(self) -> None:
        st = self.kernel.get_telemetry_stats()
        self.query_one("#k-tokens", Kpi).set_value(fmt_num(st["tokens_saved"]))
        self.query_one("#k-usd", Kpi).set_value(f"${st['usd_saved']:.4f}")
        self.query_one("#k-ratio", Kpi).set_value(f"{st['compression_ratio']:.1f}x")
        self.query_one("#k-graph", Kpi).set_value(f"{fmt_num(st['files_indexed'])} / {fmt_num(st['graph']['nodes'])}")
        self.query_one("#k-scars", Kpi).set_value(str(st["graph"]["scars"]))
        self.query_one("#k-secrets", Kpi).set_value(str(st["secrets_blocked"]))
        self.query_one("#k-swarm", Kpi).set_value(f"{len(st['swarm']['active'])} active / {st['swarm']['runs']}")
        series = [float(p["saved_tokens"]) for p in st["series"]] or [0.0]
        self.query_one("#spark", Sparkline).data = series

        scars_table = self.query_one("#scars", DataTable)
        scars_table.clear()
        scars = sorted(self.kernel.db.list_scars(300), key=lambda s: (-s.hit_count, s.severity))
        for s in scars[:40]:
            scars_table.add_row(
                Text(s.severity.upper(), style=SEVERITY_STYLES.get(s.severity, "")),
                s.file_path, s.error_summary[:70], str(s.hit_count), s.source,
            )
        snaps_table = self.query_one("#snaps", DataTable)
        snaps_table.clear()
        for snap in self.kernel.snapshots.list()[:20]:
            snaps_table.add_row(
                snap["id"], snap["label"][:40], str(snap["file_count"]), f"{snap['total_bytes'] / 1024:.0f} KB",
                datetime.fromtimestamp(snap["created_at"]).strftime("%m-%d %H:%M:%S"),
            )

    def poll_events(self) -> None:
        rows = self.kernel.db.events_since(self.last_event_id, 200)
        if not rows:
            return
        feed = self.query_one("#feed", RichLog)
        for ev in rows:
            self.last_event_id = max(self.last_event_id, ev["id"])
            self.events_seen += 1
            line = Text()
            line.append(datetime.fromtimestamp(ev["ts"]).strftime("%H:%M:%S "), style="#3a4658")
            line.append(f"{ev['type']:<19}", style=EVENT_STYLES.get(ev["type"], "#94a3b8"))
            line.append(" " + describe_event(ev), style="#d8e3f0")
            feed.write(line)

    # ------------------------------------------------------------------ actions
    def _background(self, label: str, fn: Any) -> None:
        if self.busy:
            self.notify("Another operation is running", severity="warning")
            return
        self.busy = True

        def work() -> None:
            t0 = time.perf_counter()
            try:
                result = fn()
                self.call_from_thread(self.notify, f"{label} done in {time.perf_counter() - t0:.1f}s: {result}")
            except Exception as exc:  # noqa: BLE001 - surface any failure to the user
                self.call_from_thread(self.notify, f"{label} failed: {exc}", severity="error")
            finally:
                self.busy = False
                self.call_from_thread(self.refresh_all)

        self.run_worker(work, thread=True, exclusive=True)

    def action_reindex(self) -> None:
        self._background("Index", lambda: f"{self.kernel.index()['counts']['files']} files")

    def action_snapshot(self) -> None:
        self._background("Snapshot", lambda: self.kernel.create_snapshot("tui")["id"])

    def action_scan(self) -> None:
        self._background("Security scan", lambda: self.kernel.scan_security(".")["risk_level"])

    def action_clear_feed(self) -> None:
        self.query_one("#feed", RichLog).clear()


def run_tui(root: Path | str | None = None) -> None:
    AetherTUI(root).run()
