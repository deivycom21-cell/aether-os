"""``aether`` command line interface (Typer + Rich)."""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from aether_core import __version__

app = typer.Typer(
    name="aether",
    help="AetherOS - local-first graph memory, secret shield, swarm orchestrator and 3D telemetry for AI coding agents.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()

RootOpt = Annotated[Optional[Path], typer.Option("--root", "-r", help="Project root (defaults to the nearest .aether/.git).")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print raw JSON.")]
SEV_STYLE = {"critical": "bold red", "high": "dark_orange", "medium": "yellow", "low": "grey50", "info": "grey50"}


def _kernel(root: Path | None) -> Any:
    from aether_core.kernel import AetherKernel

    return AetherKernel(root)


def _print_json(data: Any) -> None:
    console.print_json(json.dumps(data, ensure_ascii=False, default=str))


def _ago(ts: float) -> str:
    delta = time.time() - ts
    if delta < 90:
        return f"{int(delta)}s ago"
    if delta < 5400:
        return f"{int(delta / 60)}m ago"
    if delta < 172800:
        return f"{int(delta / 3600)}h ago"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _index_report(summary: dict[str, Any]) -> None:
    t = Table(title="Index", title_style="bold cyan", border_style="cyan")
    for col in ("repo", "scanned", "indexed", "unchanged", "removed", "symbols", "ms"):
        t.add_column(col, justify="right" if col != "repo" else "left")
    for r in summary["repos"]:
        t.add_row(r["repo"], str(r["scanned"]), str(r["indexed"]), str(r["unchanged"]), str(r["removed"]), str(r["nodes"]), str(r["duration_ms"]))
    console.print(t)
    c = summary["counts"]
    console.print(
        f"[cyan]graph[/] {c['files']} files · {c['nodes']} symbols · {c['edges']} edges "
        f"({c['resolved_edges']} resolved) · {c['routes']} routes · {c['scars']} scars · [dim]{summary['duration_ms']} ms[/]"
    )
    for p in summary.get("plugins", []):
        status = "[green]ok[/]" if p["ok"] else f"[red]failed[/] {p['error']}"
        skipped = " [dim](skipped)[/]" if p["summary"].get("skipped") else ""
        console.print(f"  plugin [violet]{p['name']}[/] {status}{skipped} [dim]{p['duration_ms']} ms[/]")
    for r in summary["repos"]:
        for err in r["errors"][:5]:
            console.print(f"  [yellow]warn[/] {err}")


# ---------------------------------------------------------------------------- setup


@app.command()
def init(
    root: RootOpt = None,
    link_repo: Annotated[Optional[list[Path]], typer.Option("--link-repo", "-l", help="Sibling repository to link (repeatable).")] = None,
    no_plugins: Annotated[bool, typer.Option("--no-plugins", help="Skip built-in plugins.")] = False,
    force: Annotated[bool, typer.Option("--force", help="Re-parse every file.")] = False,
) -> None:
    """Create .aether/, link sibling repos and build the code graph."""
    from aether_core.kernel import AetherKernel

    k = AetherKernel(root or Path.cwd())
    for lr in link_repo or []:
        entry = k.link_repo(lr)
        console.print(f"[violet]linked[/] {entry['name']} -> {entry['path']}")
    with console.status("[cyan]Indexing the multi-repo graph...[/]"):
        summary = k.index(force=force, run_plugins=not no_plugins)
    console.print(Panel.fit(Text.from_markup(f"[bold cyan]AETHER//OS[/] initialised at [white]{k.root}[/]\n"
                                             f"database: {k.aether_dir / 'brain.db'}"), border_style="cyan"))
    _index_report(summary)
    console.print("\nNext: [bold]aether install-mcp[/] to connect Claude Code / Cursor, then [bold]aether web --open[/].")


@app.command()
def index(
    root: RootOpt = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    no_plugins: Annotated[bool, typer.Option("--no-plugins")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Incrementally re-index the project and linked repos."""
    k = _kernel(root)
    summary = k.index(force=force, run_plugins=not no_plugins)
    _print_json(summary) if as_json else _index_report(summary)


@app.command()
def link(path: Path, name: Annotated[Optional[str], typer.Option("--name")] = None, root: RootOpt = None) -> None:
    """Link a sibling repository (e.g. frontend + backend) into the graph."""
    k = _kernel(root)
    entry = k.link_repo(path, name)
    summary = k.index(run_plugins=False)
    console.print(f"[violet]linked[/] {entry['name']} -> {entry['path']}")
    _index_report(summary)


@app.command()
def unlink(name: str, root: RootOpt = None) -> None:
    """Remove a linked repository from the graph."""
    ok = _kernel(root).unlink_repo(name)
    console.print("[green]unlinked[/]" if ok else f"[yellow]no linked repo named {name}[/]")


@app.command()
def repos(root: RootOpt = None) -> None:
    """List indexed repositories."""
    t = Table(border_style="cyan")
    for col in ("name", "files", "primary", "root", "indexed"):
        t.add_column(col)
    for r in _kernel(root).db.list_repos():
        t.add_row(r["name"], str(r["file_count"]), "yes" if r["is_primary"] else "", r["root_path"], _ago(r["indexed_at"]) if r["indexed_at"] else "-")
    console.print(t)


# ---------------------------------------------------------------------------- memory


@app.command()
def query(
    text: str,
    max_tokens: Annotated[int, typer.Option("--max-tokens", "-t")] = 1500,
    root: RootOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Hybrid search + compressed context package (what the MCP tool returns)."""
    k = _kernel(root)
    pkg = k.query_smart_context(text, max_tokens)
    if as_json:
        _print_json(pkg)
        return
    m = pkg["metrics"]
    for w in pkg["warnings"]:
        console.print(f"[dark_orange]{w}[/]")
    for r in pkg["results"]:
        title = f"[bold]{r['kind']}[/] [cyan]{r['symbol']}[/]  [dim]{r['repo']}:{r['file']}:{r['lines'][0]}-{r['lines'][1]}[/]"
        body = (r["signature"] + ("\n" + r["doc"] if r.get("doc") else "") + ("\n\n" + r["snippet"] if r.get("snippet") else "")).strip()
        console.print(Panel(Text(body), title=title, title_align="left", border_style="grey30"))
    for rel in pkg["relations"]:
        console.print(f"[violet]{rel['file']}[/] depends on {len(rel['depends_on'])}, used by {rel['used_by_count']}"
                      + (f", cross-repo: {', '.join(rel['cross_repo_consumers'])}" if rel["cross_repo_consumers"] else ""))
    for s in pkg["scars"]:
        console.print(f"[{SEV_STYLE.get(s['severity'], '')}]scar[{s['severity']}][/] {s['file']}: {s['error']} -> {s['fix']}")
    console.print(
        f"\n[green]{m['raw_tokens']:,} -> {m['compressed_tokens']:,} tokens (-{m['savings_pct']}%)[/] · saved "
        f"${m['saved_usd']['sonnet']:.4f} (Sonnet) / ${m['saved_usd']['opus']:.4f} (Opus) · {m['latency_ms']} ms"
    )


@app.command()
def impact(file: str, root: RootOpt = None, as_json: JsonOpt = False) -> None:
    """Blast radius of changing FILE (dependents, endpoints, linked repos)."""
    r = _kernel(root).impact_analysis(file)
    if as_json or not r.get("found"):
        _print_json(r)
        return
    style = {"low": "green", "medium": "yellow", "high": "dark_orange", "critical": "bold red"}[r["risk_level"]]
    console.print(Panel.fit(f"[{style}]{r['risk_level'].upper()}[/] risk ({r['risk_score']}) · {r['recommendation']}",
                            title=f"impact · {r['repo']}:{r['file']}", border_style=style))
    t = Table(border_style="grey30")
    t.add_column("depth", justify="right")
    t.add_column("dependent")
    t.add_column("repo")
    t.add_column("via")
    for d in r["transitive_dependents"][:40]:
        t.add_row(str(d["depth"]), d["file"], d["repo"], d["via"])
    if r["transitive_dependents"]:
        console.print(t)
    for e in r["endpoints_defined"]:
        console.print(f"[cyan]endpoint[/] {e['method']} {e['path']} -> {e['handler']}")
    for c in r["endpoint_consumers"]:
        console.print(f"[violet]consumer[/] {c['repo']}:{c['file']} calls {c['call']}")


@app.command()
def scar(
    file: str,
    error: str,
    fix: Annotated[str, typer.Option("--fix", "-f", help="How it was fixed.")] = "",
    severity: Annotated[str, typer.Option("--severity", "-s")] = "high",
    cause: Annotated[str, typer.Option("--cause", help="Root cause.")] = "",
    symbol: Annotated[str, typer.Option("--symbol")] = "",
    root: RootOpt = None,
) -> None:
    """Record a scar (lesson learned) on FILE."""
    r = _kernel(root).record_scar(file, error, fix, severity, cause, symbol)
    console.print(f"[green]scar #{r['id']} recorded[/] on {r['file_path']}" if r.get("ok") else f"[red]{r.get('error')}[/]")


@app.command()
def scars(file: Annotated[Optional[str], typer.Argument()] = None, root: RootOpt = None, as_json: JsonOpt = False) -> None:
    """List scars (all, or for one file)."""
    k = _kernel(root)
    items = k.check_scars(file) if file else [s.to_dict() for s in k.db.list_scars()]
    if as_json:
        _print_json(items)
        return
    t = Table(border_style="dark_orange")
    for col in ("id", "sev", "file", "error", "fix", "src", "hits"):
        t.add_column(col)
    for s in items:
        t.add_row(str(s["id"]), Text(s["severity"], style=SEV_STYLE.get(s["severity"], "")), s["file_path"], s["error_summary"][:60],
                  s["fix_applied"][:50], s["source"], str(s["hit_count"]))
    console.print(t)


@app.command()
def decision(
    title: str,
    body: str,
    tag: Annotated[Optional[list[str]], typer.Option("--tag")] = None,
    root: RootOpt = None,
) -> None:
    """Record an architecture decision (shared via the team brain)."""
    ok = _kernel(root).db.add_decision(title, body, tag or [])
    console.print("[green]decision recorded[/]" if ok else "[yellow]a decision with that title already exists[/]")


# ---------------------------------------------------------------------------- time travel


@app.command()
def snapshot(label: Annotated[str, typer.Argument()] = "manual", root: RootOpt = None) -> None:
    """Take a time-travel snapshot of all tracked files."""
    s = _kernel(root).create_snapshot(label)
    console.print(f"[green]snapshot[/] [bold]{s['id']}[/] '{s['label']}' · {s['file_count']} files · "
                  f"{s['total_bytes'] / 1024:.0f} KB · {s['new_objects']} new blobs")


@app.command()
def snapshots(root: RootOpt = None) -> None:
    """List snapshots."""
    t = Table(border_style="green")
    for col in ("id", "label", "files", "size", "when"):
        t.add_column(col)
    for s in _kernel(root).snapshots.list():
        t.add_row(s["id"], s["label"], str(s["file_count"]), f"{s['total_bytes'] / 1024:.0f} KB", _ago(s["created_at"]))
    console.print(t)


@app.command()
def diff(snapshot_id: str, root: RootOpt = None) -> None:
    """Show what changed since a snapshot."""
    d = _kernel(root).snapshots.diff(snapshot_id)
    for kind, style in (("modified", "yellow"), ("added", "green"), ("removed", "red")):
        for p in d[kind]:
            console.print(f"[{style}]{kind[0].upper()}[/] {p}")
    if not any(d.values()):
        console.print("[green]no changes since snapshot[/]")


@app.command()
def rollback(
    snapshot_id: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    keep_new: Annotated[bool, typer.Option("--keep-new", help="Keep files created after the snapshot.")] = False,
    root: RootOpt = None,
) -> None:
    """Restore the project to SNAPSHOT_ID (a safety snapshot is taken first)."""
    k = _kernel(root)
    d = k.snapshots.diff(snapshot_id)
    console.print(f"{len(d['modified'])} modified, {len(d['removed'])} removed, {len(d['added'])} added since {snapshot_id}")
    if not any(d.values()):
        console.print("[green]already at that state[/]")
        return
    if not yes and not typer.confirm("Restore now?"):
        raise typer.Abort()
    r = k.snapshots.rollback(snapshot_id, prune_new=not keep_new)
    if r["restored"] or r["deleted_new_files"]:
        k.index(run_plugins=False)
    console.print(f"[green]restored {r['restored']} file(s)[/], removed {r['deleted_new_files']} new file(s); "
                  f"safety snapshot: [bold]{r['safety_snapshot']}[/]")


# ---------------------------------------------------------------------------- security


@app.command()
def scan(target: Annotated[str, typer.Argument()] = ".", root: RootOpt = None, as_json: JsonOpt = False) -> None:
    """Scan for leaked secrets and prompt-injection payloads."""
    r = _kernel(root).scan_security(target)
    if as_json or "error" in r:
        _print_json(r)
        if r.get("risk_level") in {"critical", "high"} or "error" in r:
            raise typer.Exit(1)
        return
    style = {"clean": "green", "medium": "yellow", "high": "dark_orange", "critical": "bold red"}[r["risk_level"]]
    console.print(Panel.fit(f"[{style}]{r['risk_level'].upper()}[/] · {r['files_scanned']} files · "
                            f"{r['secret_count']} secrets · {r['injection_count']} injection payloads", border_style=style))
    if r["secrets"]:
        t = Table(title="secrets (values masked)", border_style="red")
        for col in ("file", "line", "rule", "preview"):
            t.add_column(col)
        for s in r["secrets"][:100]:
            t.add_row(s["file"], str(s["line"]), s["rule"], s["preview"])
        console.print(t)
    for i in r["injections"][:50]:
        rules = ", ".join(sorted({f["rule"] for f in i["findings"]}))
        console.print(f"[red]injection[/] {i['file']} [{i['severity']}] score={i['score']} · {rules}")
    if r["risk_level"] in {"critical", "high"}:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------- telemetry & UIs


@app.command()
def stats(root: RootOpt = None, as_json: JsonOpt = False) -> None:
    """Token/USD savings, graph size and security counters."""
    s = _kernel(root).get_telemetry_stats()
    if as_json:
        _print_json(s)
        return
    t = Table(show_header=False, border_style="cyan", title=f"AETHER//OS · {s['project']}", title_style="bold cyan")
    t.add_column(style="grey62")
    t.add_column(style="bold")
    rows = [
        ("tokens saved", f"{s['tokens_saved']:,}"), ("usd saved", f"${s['usd_saved']:.4f}"),
        ("queries", str(s["queries"])), ("compression", f"{s['compression_ratio']}x"),
        ("avg latency", f"{s['avg_latency_ms']} ms"), ("files indexed", str(s["files_indexed"])),
        ("symbols", str(s["graph"]["nodes"])), ("edges", f"{s['graph']['edges']} ({s['graph']['resolved_edges']} resolved)"),
        ("routes", str(s["graph"]["routes"])), ("scars", str(s["graph"]["scars"])),
        ("snapshots", str(s["graph"]["snapshots"])), ("secrets blocked", str(s["secrets_blocked"])),
        ("injections", str(s["injections_detected"])), ("linked repos", ", ".join(s["linked_repos"]) or "-"),
        ("swarm runs", f"{s['swarm']['runs']} ({len(s['swarm']['active'])} active)"),
    ]
    for a, b in rows:
        t.add_row(a, b)
    console.print(t)


@app.command()
def mcp(root: RootOpt = None, no_auto_index: Annotated[bool, typer.Option("--no-auto-index")] = False) -> None:
    """Run the MCP server over stdio (used by Claude Code / Cursor / Windsurf)."""
    from aether_core.mcp_server import run_stdio

    run_stdio(root, auto_index=not no_auto_index)


@app.command("install-mcp")
def install_mcp(
    client: Annotated[str, typer.Option("--client", "-c", help="claude | cursor | windsurf | all")] = "all",
    print_only: Annotated[bool, typer.Option("--print", help="Only print the configuration.")] = False,
    root: RootOpt = None,
) -> None:
    """Register the Aether MCP server for Claude Code (.mcp.json) and Cursor (.cursor/mcp.json)."""
    from aether_core.kernel import find_project_root

    project = (root or find_project_root()).resolve()
    server = {"command": sys.executable, "args": ["-m", "aether_core.cli", "mcp", "--root", str(project)]}
    targets: list[tuple[str, Path]] = []
    if client in {"claude", "all"}:
        targets.append(("Claude Code", project / ".mcp.json"))
    if client in {"cursor", "all"}:
        targets.append(("Cursor", project / ".cursor" / "mcp.json"))
    snippet = json.dumps({"mcpServers": {"aether": server}}, indent=2)
    if print_only or client == "windsurf":
        typer.echo(snippet)  # plain output: Rich would wrap long paths and break copy-paste
        if client in {"windsurf", "all"}:
            console.print("[dim]Windsurf: add the block above to ~/.codeium/windsurf/mcp_config.json[/]")
        return
    for name, path in targets:
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                console.print(f"[red]{path} is not valid JSON; not modified[/]")
                continue
        data.setdefault("mcpServers", {})["aether"] = server
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]{name}[/] -> {path}")
    if client == "all":
        console.print("[dim]Windsurf: run `aether install-mcp --client windsurf` to print its snippet. "
                      "Aider: use `aether query \"...\" --json` via /run.[/]")


@app.command()
def web(
    root: RootOpt = None,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p")] = 7777,
    open_browser: Annotated[bool, typer.Option("--open", help="Open the browser.")] = False,
) -> None:
    """Serve the 3D digital twin dashboard + REST + WebSocket telemetry."""
    from aether_web.server import run_server

    console.print(f"[cyan]AETHER//OS dashboard[/] -> http://{host}:{port}/  [dim](Ctrl+C to stop)[/]")
    run_server(root, host, port, open_browser)


@app.command()
def tui(root: RootOpt = None) -> None:
    """Launch the cyberpunk terminal dashboard."""
    from aether_tui.app import run_tui

    run_tui(root)


@app.command()
def watch(root: RootOpt = None, debounce: Annotated[float, typer.Option("--debounce")] = 0.6) -> None:
    """Watch files and re-index incrementally on every change."""
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    k = _kernel(root)
    if k.db.counts()["files"] == 0:
        k.index()
    pending: set[Path] = set()
    lock = threading.Lock()
    skip = {".git", ".aether", "node_modules", ".venv", "__pycache__"}

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event: FileSystemEvent) -> None:
            if event.is_directory:
                return
            for raw in (event.src_path, getattr(event, "dest_path", "")):
                if not raw:
                    continue
                p = Path(str(raw))
                if skip.intersection(p.parts):
                    continue
                with lock:
                    pending.add(p)

    observer = Observer()
    roots = [k.root] + [Path(r["path"]) for r in k.config.linked_repos if Path(r["path"]).is_dir()]
    for r in roots:
        observer.schedule(Handler(), str(r), recursive=True)
    observer.start()
    console.print(f"[cyan]watching[/] {', '.join(str(r) for r in roots)} [dim](Ctrl+C to stop)[/]")
    try:
        while True:
            time.sleep(debounce)
            with lock:
                batch = list(pending)
                pending.clear()
            if batch:
                changed = k.reindex_paths(batch)
                if changed:
                    console.print(f"[dim]{datetime.now():%H:%M:%S}[/] re-indexed {changed} file(s)")
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


# ---------------------------------------------------------------------------- swarm


def _swarm(root: Path | None) -> Any:
    from aether_swarm.orchestrator import SwarmOrchestrator

    return SwarmOrchestrator(_kernel(root))


def _swarm_table(run: dict[str, Any]) -> None:
    t = Table(title=f"swarm {run['id']} · {run['status']}", title_style="bold violet", border_style="violet")
    for col in ("role", "tier", "model", "status", "commits", "worktree"):
        t.add_column(col)
    for role, st in run["roles"].items():
        t.add_row(role, st["routing"]["tier"], st["routing"]["model"], st["status"], str(st.get("commits", 0)), st["worktree"])
    console.print(t)
    if run.get("cost_estimate"):
        c = run["cost_estimate"]
        console.print(f"[dim]estimated cost ${c['total_usd']:.4f} vs ${c['baseline_usd']:.4f} all-architect baseline[/]")
    for n in run.get("notes", []):
        console.print(f"[yellow]note[/] {n}")


@app.command()
def swarm(
    description: str,
    roles: Annotated[str, typer.Option("--roles", help="Comma-separated roles.")] = "architect,builder,tester,security_auditor",
    execute: Annotated[bool, typer.Option("--execute", help="Run roles with your own LLM credentials (BYOK).")] = False,
    root: RootOpt = None,
) -> None:
    """Launch a swarm: one isolated git worktree + brief per role."""
    from aether_swarm.orchestrator import SwarmError

    try:
        run = _swarm(root).launch(description, tuple(r.strip() for r in roles.split(",") if r.strip()), execute=execute)
    except SwarmError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    _swarm_table(run)
    console.print(f"Briefs: {Path(next(iter(run['roles'].values()))['brief']).parent}")
    console.print(f"Next: work in each worktree, then [bold]aether swarm-verify {run['id']}[/]")


@app.command("swarm-status")
def swarm_status(run_id: Annotated[Optional[str], typer.Argument()] = None, root: RootOpt = None) -> None:
    """Show one swarm run (or list all)."""
    s = _swarm(root)
    if run_id:
        _swarm_table(s.status(run_id))
        return
    t = Table(border_style="violet")
    for col in ("id", "status", "description", "created"):
        t.add_column(col)
    for r in s.list_runs():
        t.add_row(r["id"], r["status"], r["description"][:60], _ago(r["created_at"]))
    console.print(t)


@app.command("swarm-execute")
def swarm_execute(run_id: str, root: RootOpt = None) -> None:
    """Run the roles of a launched swarm with your LLM credentials."""
    _swarm_table(_swarm(root).execute(run_id))


@app.command("swarm-verify")
def swarm_verify(run_id: str, no_tests: Annotated[bool, typer.Option("--no-tests")] = False, root: RootOpt = None) -> None:
    """Integrate role branches, run tests and a security scan."""
    run = _swarm(root).verify(run_id, run_tests=not no_tests)
    v = run["verification"]
    style = "green" if v["ok"] else "red"
    console.print(Panel.fit(
        f"[{style}]{run['status'].upper()}[/] · merged roles: {', '.join(v['merged_roles']) or '-'} · "
        f"{len(v['changed_files'])} changed files · conflicts {len(v['conflicts'])} · secrets {len(v['secrets'])} · "
        f"tests {'passed' if v['tests'].get('passed') else 'n/a' if not v['tests'].get('ran') else 'FAILED'}",
        border_style=style))
    if v["tests"].get("ran") and not v["tests"].get("passed"):
        console.print(v["tests"].get("output_tail", ""))
    if not v["ok"]:
        raise typer.Exit(1)


@app.command("swarm-merge")
def swarm_merge(run_id: str, force: Annotated[bool, typer.Option("--force")] = False, root: RootOpt = None) -> None:
    """Merge a verified swarm into the current branch (snapshot first)."""
    from aether_swarm.orchestrator import SwarmError

    try:
        run = _swarm(root).merge(run_id, force=force)
    except SwarmError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    console.print(f"[green]merged[/] {run_id} · pre-merge snapshot {run['pre_merge_snapshot']}")


@app.command("swarm-cleanup")
def swarm_cleanup(run_id: str, root: RootOpt = None) -> None:
    """Remove a swarm's worktrees and branches."""
    r = _swarm(root).cleanup(run_id)
    console.print(f"[green]cleaned[/] {len(r['removed_worktrees'])} worktree(s)")


@app.command()
def route(prompt: str, as_json: JsonOpt = False) -> None:
    """Show which model tier the router would pick for PROMPT and the estimated savings."""
    from aether_swarm.router import ComplexityRouter

    d = ComplexityRouter().route(prompt).to_dict()
    if as_json:
        _print_json(d)
        return
    console.print(Panel.fit(
        f"[bold violet]{d['tier']}[/] -> [cyan]{d['model']}[/]  complexity {d['complexity']}\n"
        f"est. ${d['est_cost_usd']:.5f} vs ${d['baseline_cost_usd']:.5f} on {d['baseline_model']} "
        f"([green]-{d['savings_pct']}%[/])\n" + "\n".join(f"- {r}" for r in d["reasons"]),
        border_style="violet"))


# ---------------------------------------------------------------------------- team brain & plugins


@app.command("export-brain")
def export_brain(out: Annotated[Optional[Path], typer.Option("--out")] = None, root: RootOpt = None) -> None:
    """Export the shareable team brain (.aether/team_brain.json.gz). Never includes secrets."""
    s = _kernel(root).export_brain(out)
    console.print(f"[green]exported[/] {s['path']} · {s['files']} files · {s['scars']} scars · {s['decisions']} decisions · "
                  f"{s['bytes'] / 1024:.1f} KB · {s['secrets_masked']} secret(s) masked")


@app.command("import-brain")
def import_brain(path: Annotated[Optional[Path], typer.Option("--path")] = None, root: RootOpt = None) -> None:
    """Merge a teammate's team brain (scars + decisions)."""
    s = _kernel(root).import_brain(path)
    console.print(f"[green]imported[/] {s['scars_imported']} scars, {s['decisions_imported']} decisions from {s['path']}")


@app.command()
def plugins(run: Annotated[Optional[str], typer.Option("--run", help="Run one plugin by name.")] = None, root: RootOpt = None) -> None:
    """List (or run) plugins."""
    from plugins.base import PluginManager

    k = _kernel(root)
    pm = PluginManager(k)
    if run:
        r = pm.run_named(run)
        _print_json(r.to_dict())
        if not r.ok:
            raise typer.Exit(1)
        return
    t = Table(border_style="violet")
    for col in ("name", "version", "description"):
        t.add_column(col)
    for p in pm.describe():
        t.add_row(p["name"], p["version"], p["description"])
    console.print(t)
    for e in pm.load_errors:
        console.print(f"[yellow]{e}[/]")


@app.command()
def benchmark(root: RootOpt = None) -> None:
    """Run the token-savings/latency benchmark on this project."""
    import importlib.util

    script = Path(__file__).resolve().parent.parent / "benchmarks" / "run_benchmark.py"
    if not script.is_file():
        console.print("[red]benchmarks/run_benchmark.py not found (benchmarks ship with the source checkout)[/]")
        raise typer.Exit(1)
    spec = importlib.util.spec_from_file_location("aether_benchmark", script)
    if spec is None or spec.loader is None:
        raise typer.Exit(1)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main(root)


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"aether-os {__version__}")


if __name__ == "__main__":
    app()
