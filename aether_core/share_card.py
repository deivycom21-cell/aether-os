"""Shareable savings card: a self-contained 1200x630 SVG (Open Graph size) with real AetherOS metrics.

No external fonts, images or scripts: system font stacks only, so the file renders identically when
uploaded to X / Reddit / GitHub or opened offline.
"""

from __future__ import annotations

import time
from typing import Any
from xml.sax.saxutils import escape

WIDTH, HEIGHT = 1200, 630
BG, CYAN, VIOLET, TEXT, MUTED, GREEN = "#05070d", "#00f0ff", "#8b5cf6", "#d8e3f0", "#6b7a90", "#10b981"
MONO = "ui-monospace, 'SFMono-Regular', 'Cascadia Code', Consolas, 'Liberation Mono', Menlo, monospace"
SANS = "'Segoe UI', system-ui, -apple-system, 'Helvetica Neue', Arial, sans-serif"


def human_int(n: float) -> str:
    n = float(n)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            v = n / div
            return f"{v:.1f}{suffix}" if v < 100 else f"{v:.0f}{suffix}"
    return f"{n:,.0f}"


def human_usd(v: float) -> str:
    if v >= 100:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:,.2f}"
    return f"${v:.4f}"


def _sparkline(series: list[float], x: int, y: int, w: int, h: int) -> str:
    if len(series) < 2:
        return (f'<text x="{x}" y="{y + h // 2}" fill="{MUTED}" font-family="{MONO}" font-size="16">'
                "savings per query · run a few queries to draw the curve</text>")
    mx = max(series) or 1.0
    step = w / (len(series) - 1)
    pts = [(x + i * step, y + h - (v / mx) * (h - 6) - 3) for i, v in enumerate(series)]
    line = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    area = f"{x},{y + h} " + line + f" {x + w},{y + h}"
    return (f'<polygon points="{area}" fill="url(#spark)"/>'
            f'<polyline points="{line}" fill="none" stroke="{CYAN}" stroke-width="3" stroke-linejoin="round" filter="url(#glow)"/>')


def render_share_card(m: dict[str, Any]) -> str:
    """``m`` keys: project, files_indexed, tokens_saved, savings_pct, usd_saved, p50_ms, queries, series, version."""
    project = escape(str(m.get("project", "project")))[:48]
    pct = float(m.get("savings_pct", 0.0))
    tiles = [
        ("TOKENS SAVED", human_int(m.get("tokens_saved", 0)), CYAN),
        ("COMPRESSION", f"{pct:.1f}%", VIOLET),
        ("USD SAVED", human_usd(float(m.get("usd_saved", 0.0))), GREEN),
        ("FILES INDEXED", human_int(m.get("files_indexed", 0)), TEXT),
        ("P50 LATENCY", f"{float(m.get('p50_ms', 0.0)):.0f} ms", TEXT),
        ("QUERIES", human_int(m.get("queries", 0)), TEXT),
    ]
    tile_svg: list[str] = []
    tw, th, gap, x0, y0 = 352, 128, 22, 64, 208
    for i, (label, value, color) in enumerate(tiles):
        col, row = i % 3, i // 3
        tx, ty = x0 + col * (tw + gap), y0 + row * (th + gap)
        glow = ' filter="url(#glow)"' if color == CYAN else ""
        tile_svg.append(
            f'<g transform="translate({tx},{ty})">'
            f'<rect width="{tw}" height="{th}" rx="14" fill="#0a0f1c" stroke="{CYAN}" stroke-opacity="0.18"/>'
            f'<text x="24" y="40" fill="{MUTED}" font-family="{MONO}" font-size="15" letter-spacing="3">{escape(label)}</text>'
            f'<text x="24" y="100" fill="{color}" font-family="{SANS}" font-weight="700" font-size="50"{glow}>'
            f'{escape(value)}</text></g>'
        )
    bar_w = 1072 * max(0.0, min(pct, 100.0)) / 100.0
    series = [float(v) for v in m.get("series", [])][-40:]
    date = escape(time.strftime("%Y-%m-%d"))
    version = escape(str(m.get("version", "")))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="AetherOS savings for {project}">
<title>AetherOS · {project} · {human_int(m.get('tokens_saved', 0))} tokens saved ({pct:.1f}% compression)</title>
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{BG}"/><stop offset="1" stop-color="#0b1020"/></linearGradient>
<radialGradient id="halo" cx="0.85" cy="0.1" r="0.6"><stop offset="0" stop-color="{VIOLET}" stop-opacity="0.35"/><stop offset="1" stop-color="{VIOLET}" stop-opacity="0"/></radialGradient>
<radialGradient id="halo2" cx="0.05" cy="1" r="0.55"><stop offset="0" stop-color="{CYAN}" stop-opacity="0.18"/><stop offset="1" stop-color="{CYAN}" stop-opacity="0"/></radialGradient>
<linearGradient id="bar" x1="0" x2="1"><stop offset="0" stop-color="{CYAN}"/><stop offset="1" stop-color="{VIOLET}"/></linearGradient>
<linearGradient id="spark" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{CYAN}" stop-opacity="0.35"/><stop offset="1" stop-color="{CYAN}" stop-opacity="0"/></linearGradient>
<pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0H0V40" fill="none" stroke="{CYAN}" stroke-opacity="0.05"/></pattern>
<filter id="glow" x="-20%" y="-50%" width="140%" height="200%"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>
<rect width="{WIDTH}" height="{HEIGHT}" fill="url(#bg)"/>
<rect width="{WIDTH}" height="{HEIGHT}" fill="url(#grid)"/>
<rect width="{WIDTH}" height="{HEIGHT}" fill="url(#halo)"/>
<rect width="{WIDTH}" height="{HEIGHT}" fill="url(#halo2)"/>
<rect x="1" y="1" width="{WIDTH - 2}" height="{HEIGHT - 2}" rx="22" fill="none" stroke="{CYAN}" stroke-opacity="0.25" stroke-width="2"/>
<text x="64" y="92" fill="{CYAN}" font-family="{SANS}" font-weight="700" font-size="40" letter-spacing="8" filter="url(#glow)">AETHER<tspan fill="{VIOLET}">//</tspan>OS</text>
<text x="64" y="140" fill="{TEXT}" font-family="{MONO}" font-size="24">{project}</text>
<text x="64" y="172" fill="{MUTED}" font-family="{MONO}" font-size="16">local-first context kernel for AI coding agents · $0 infra · BYOK</text>
<text x="{WIDTH - 64}" y="92" text-anchor="end" fill="{MUTED}" font-family="{MONO}" font-size="16">v{version} · {date}</text>
{''.join(tile_svg)}
<text x="64" y="530" fill="{MUTED}" font-family="{MONO}" font-size="14" letter-spacing="3">CONTEXT SENT VS. FULL FILES</text>
<rect x="64" y="542" width="1072" height="10" rx="5" fill="#111827"/>
<rect x="64" y="542" width="{bar_w:.1f}" height="10" rx="5" fill="url(#bar)" filter="url(#glow)"/>
<g opacity="0.9">{_sparkline(series, 820, 494, 316, 34)}</g>
<text x="64" y="596" fill="{MUTED}" font-family="{MONO}" font-size="15">github.com/deivycom21-cell/aether-os · metrics measured locally, nothing leaves the machine</text>
</svg>
"""
