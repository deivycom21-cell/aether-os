"""Real benchmark on a repository (this one by default): indexing time, query latency and token savings.

Usage::

    .venv\\Scripts\\python.exe benchmarks\\run_benchmark.py [--root PATH] [--json OUT]
    aether benchmark

It uses a throw-away database so your real telemetry is not polluted. "raw tokens" is what an agent
would spend reading the full files that contain the hits; "compressed" is the Aether package.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from aether_core.context_engine import REFERENCE_MODELS, tokens_to_usd  # noqa: E402
from aether_core.kernel import AetherKernel  # noqa: E402

DEFAULT_QUERIES = [
    "how are secrets masked before reaching the agent",
    "shannon entropy threshold for secret detection",
    "prompt injection hidden in html comments",
    "rollback snapshot restore files content addressed",
    "resolve cross repo http calls to backend routes",
    "fastapi route decorator with router prefix",
    "export team brain gzip without secrets",
    "websocket telemetry stream events to dashboard",
    "complexity router choose ollama haiku sonnet opus",
    "git worktree swarm verify and merge",
    "record scar lesson learned after bug fix",
    "token estimator calibrated without tiktoken",
    "plugin loader static audit forbidden imports",
    "impact analysis transitive dependents risk score",
    "hybrid search bm25 vectors reciprocal rank fusion",
]


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def run(root: Path, queries: list[str], max_tokens: int = 1500) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="aether-bench-") as tmp:
        kernel = AetherKernel(root, db_path=Path(tmp) / "bench.db")
        t0 = time.perf_counter()
        summary = kernel.index(force=True, run_plugins=False)
        cold_index_ms = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        kernel.index(run_plugins=False)
        warm_index_ms = (time.perf_counter() - t1) * 1000
        repo_tokens = sum(f.token_estimate for f in kernel.db.list_files())

        rows = []
        first_query_ms = None
        for q in queries:
            t = time.perf_counter()
            pkg = kernel.query_smart_context(q, max_tokens=max_tokens)
            ms = (time.perf_counter() - t) * 1000
            if first_query_ms is None:
                first_query_ms = ms  # includes building the vector index
            m = pkg["metrics"]
            rows.append({
                "query": q, "latency_ms": round(ms, 2), "raw_tokens": m["raw_tokens"],
                "compressed_tokens": m["compressed_tokens"], "saved_tokens": m["saved_tokens"],
                "savings_pct": m["savings_pct"], "top_hit": pkg["results"][0]["symbol"] if pkg["results"] else "",
            })
        counts = kernel.db.counts()
        kernel.close()

    warm = [r["latency_ms"] for r in rows[1:]] or [r["latency_ms"] for r in rows]
    raw = sum(r["raw_tokens"] for r in rows)
    comp = sum(r["compressed_tokens"] for r in rows)
    saved = sum(r["saved_tokens"] for r in rows)
    return {
        "root": str(root),
        "files": counts["files"], "symbols": counts["nodes"], "edges": counts["edges"],
        "resolved_edges": counts["resolved_edges"], "repo_tokens": repo_tokens,
        "cold_index_ms": round(cold_index_ms, 1), "warm_index_ms": round(warm_index_ms, 1),
        "index_files_per_s": round(summary["counts"]["files"] / (cold_index_ms / 1000), 1) if cold_index_ms else 0,
        "first_query_ms": round(first_query_ms or 0, 2),
        "query_p50_ms": round(percentile(warm, 50), 2), "query_p95_ms": round(percentile(warm, 95), 2),
        "queries": rows,
        "total_raw_tokens": raw, "total_compressed_tokens": comp, "total_saved_tokens": saved,
        "overall_savings_pct": round(saved / raw * 100, 2) if raw else 0.0,
        "saved_usd": {alias: round(tokens_to_usd(saved, model), 4) for alias, model in REFERENCE_MODELS.items()},
        "max_tokens": max_tokens,
    }


def render(result: dict[str, Any], console: Console) -> None:
    t = Table(title="AetherOS benchmark · per query", title_style="bold cyan", border_style="cyan")
    for col, j in (("query", "left"), ("top hit", "left"), ("raw", "right"), ("aether", "right"), ("saved", "right"), ("ms", "right")):
        t.add_column(col, justify=j)  # type: ignore[arg-type]
    for r in result["queries"]:
        t.add_row(r["query"][:44], r["top_hit"][-38:], f"{r['raw_tokens']:,}", f"{r['compressed_tokens']:,}",
                  f"{r['savings_pct']}%", f"{r['latency_ms']:.1f}")
    console.print(t)
    s = Table(show_header=False, border_style="violet", title="summary", title_style="bold violet")
    s.add_column(style="grey62")
    s.add_column(style="bold")
    for k, v in (
        ("repository", result["root"]),
        ("graph", f"{result['files']} files · {result['symbols']} symbols · {result['edges']} edges ({result['resolved_edges']} resolved)"),
        ("whole repo", f"{result['repo_tokens']:,} tokens"),
        ("index (cold / warm)", f"{result['cold_index_ms']:.0f} ms / {result['warm_index_ms']:.0f} ms ({result['index_files_per_s']} files/s)"),
        ("query latency", f"first {result['first_query_ms']:.0f} ms (builds vectors) · p50 {result['query_p50_ms']} ms · p95 {result['query_p95_ms']} ms"),
        ("tokens", f"{result['total_raw_tokens']:,} raw -> {result['total_compressed_tokens']:,} sent ({result['overall_savings_pct']}% saved)"),
        ("usd saved", f"${result['saved_usd']['sonnet']} Sonnet 5.5 · ${result['saved_usd']['opus']} Opus 5.5 (input pricing)"),
    ):
        s.add_row(k, v)
    console.print(s)


def main(root: Path | str | None = None, out: Path | None = None, queries: list[str] | None = None) -> dict[str, Any]:
    console = Console()
    target = Path(root).resolve() if root else ROOT
    with console.status(f"[cyan]benchmarking {target}...[/]"):
        result = run(target, queries or DEFAULT_QUERIES)
    render(result, console)
    out = out or (ROOT / "benchmarks" / "results.json")
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    console.print(f"[dim]results written to {out}[/]")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None, dest="out")
    args = parser.parse_args()
    main(args.root, args.out)
