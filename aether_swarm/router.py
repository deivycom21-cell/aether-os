"""Complexity router: picks the cheapest model tier that can handle a task.

Tiers
-----
* ``LOCAL_OLLAMA``  – formatting, docs, comments, renames (free, only if Ollama answers on :11434)
* ``FAST_MODEL``    – targeted edits (Claude Haiku)
* ``BALANCED_MODEL``– features and multi-file fixes (Claude Sonnet)
* ``ARCHITECT_MODEL`` – architecture, migrations, security, concurrency (Claude Opus)

Model ids are overridable with ``AETHER_FAST_MODEL`` / ``AETHER_BALANCED_MODEL`` /
``AETHER_ARCHITECT_MODEL`` / ``AETHER_OLLAMA_MODEL``; the Ollama URL with ``AETHER_OLLAMA_URL``.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from aether_core.context_engine import MODEL_PRICES_PER_MTOK, estimate_tokens

DEFAULT_OLLAMA_URL = "http://localhost:11434"


class Tier(str, Enum):
    LOCAL_OLLAMA = "LOCAL_OLLAMA"
    FAST_MODEL = "FAST_MODEL"
    BALANCED_MODEL = "BALANCED_MODEL"
    ARCHITECT_MODEL = "ARCHITECT_MODEL"


def tier_models() -> dict[Tier, str]:
    return {
        Tier.LOCAL_OLLAMA: os.environ.get("AETHER_OLLAMA_MODEL", ""),
        Tier.FAST_MODEL: os.environ.get("AETHER_FAST_MODEL", "claude-haiku-5-5"),
        Tier.BALANCED_MODEL: os.environ.get("AETHER_BALANCED_MODEL", "claude-sonnet-5-5"),
        Tier.ARCHITECT_MODEL: os.environ.get("AETHER_ARCHITECT_MODEL", "claude-opus-5-5"),
    }


LOW_SIGNALS = re.compile(
    r"\b(format(?:ting)?|prettier|lint|docstrings?|docs?|documentation|readme|comments?|typos?|rename|"
    r"spelling|translate|changelog|whitespace|indent(?:ation)?|summari[sz]e|explain|tidy|sort imports)\b",
    re.I,
)
MEDIUM_SIGNALS = re.compile(
    r"\b(fix|bug|add|update|implement|endpoint|test(?:s|ing)?|validation|handler|component|feature|"
    r"refactor|optimi[sz]e|cache|pagination|error handling|logging)\b",
    re.I,
)
HIGH_SIGNALS = re.compile(
    r"\b(architect(?:ure)?|design|migrat(?:e|ion)|distributed|concurren(?:cy|t)|race condition|deadlock|"
    r"security|auth(?:entication|orization)?|cryptograph\w*|scalab\w*|performance|protocol|schema|"
    r"multi[- ]repo|orchestrat\w*|algorithm|consensus|rewrite|redesign|data model|state machine|"
    r"zero[- ]downtime|sharding|replication|threat model)\b",
    re.I,
)
FILE_MENTION = re.compile(r"\b[\w./-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|kt|rb|php|cs|sql|ya?ml|json|toml|md)\b")


@dataclass(slots=True)
class RoutingDecision:
    tier: Tier
    model: str
    complexity: float
    reasons: list[str] = field(default_factory=list)
    est_input_tokens: int = 0
    est_output_tokens: int = 0
    est_cost_usd: float = 0.0
    baseline_model: str = ""
    baseline_cost_usd: float = 0.0
    ollama_available: bool = False

    @property
    def savings_usd(self) -> float:
        return max(0.0, self.baseline_cost_usd - self.est_cost_usd)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "model": self.model,
            "complexity": round(self.complexity, 3),
            "reasons": self.reasons,
            "est_input_tokens": self.est_input_tokens,
            "est_output_tokens": self.est_output_tokens,
            "est_cost_usd": round(self.est_cost_usd, 6),
            "baseline_model": self.baseline_model,
            "baseline_cost_usd": round(self.baseline_cost_usd, 6),
            "savings_usd": round(self.savings_usd, 6),
            "savings_pct": round(self.savings_usd / self.baseline_cost_usd * 100, 1) if self.baseline_cost_usd else 0.0,
            "ollama_available": self.ollama_available,
        }


def _cost(model: str, inp: int, out: int, tier: Tier) -> float:
    if tier == Tier.LOCAL_OLLAMA:
        return 0.0
    price = MODEL_PRICES_PER_MTOK.get(model)
    if price is None:
        # Unknown override: assume the tier's default pricing.
        default = {Tier.FAST_MODEL: "claude-haiku-5-5", Tier.BALANCED_MODEL: "claude-sonnet-5-5"}.get(tier, "claude-opus-5-5")
        price = MODEL_PRICES_PER_MTOK[default]
    return (inp * price["input"] + out * price["output"]) / 1_000_000


class OllamaProbe:
    """Cached liveness probe for a local Ollama daemon (never reaches beyond the configured URL)."""

    def __init__(self, url: str | None = None, ttl: float = 30.0) -> None:
        self.url = (url or os.environ.get("AETHER_OLLAMA_URL") or DEFAULT_OLLAMA_URL).rstrip("/")
        self.ttl = ttl
        self._checked_at = 0.0
        self._models: list[str] | None = None
        self._lock = threading.Lock()

    def models(self) -> list[str] | None:
        with self._lock:
            if time.time() - self._checked_at < self.ttl:
                return self._models
            try:
                r = httpx.get(f"{self.url}/api/tags", timeout=0.6)
                r.raise_for_status()
                self._models = [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
            except (httpx.HTTPError, ValueError):
                self._models = None
            self._checked_at = time.time()
            return self._models

    def available(self) -> bool:
        return bool(self.models())


class ComplexityRouter:
    def __init__(self, ollama: OllamaProbe | None = None, context_tokens: int = 1500) -> None:
        self.ollama = ollama or OllamaProbe()
        self.context_tokens = context_tokens

    def score(self, prompt: str) -> tuple[float, list[str]]:
        reasons: list[str] = []
        score = 0.3
        low = {m.lower() for m in LOW_SIGNALS.findall(prompt)}
        med = {m.lower() for m in MEDIUM_SIGNALS.findall(prompt)}
        high = {m.lower() for m in HIGH_SIGNALS.findall(prompt)}
        if low:
            score -= min(0.45, 0.22 * len(low))
            reasons.append(f"low-complexity signals: {', '.join(sorted(low))}")
        if med:
            score += min(0.2, 0.07 * len(med))
            reasons.append(f"implementation signals: {', '.join(sorted(med))}")
        if high:
            score += min(0.6, 0.22 * len(high))
            reasons.append(f"architecture/risk signals: {', '.join(sorted(high))}")
        tokens = estimate_tokens(prompt)
        length_bonus = min(0.25, tokens / 1600)
        if length_bonus > 0.05:
            score += length_bonus
            reasons.append(f"long specification (~{tokens} tokens)")
        files = set(FILE_MENTION.findall(prompt))
        if len(files) > 1:
            score += min(0.2, 0.05 * len(files))
            reasons.append(f"touches {len(files)} files")
        if "```" in prompt:
            score += 0.05
            reasons.append("includes code blocks")
        return max(0.0, min(1.0, score)), reasons

    def route(self, prompt: str, context_tokens: int | None = None) -> RoutingDecision:
        complexity, reasons = self.score(prompt)
        models = tier_models()
        ollama_models = self.ollama.models()
        ollama_ok = bool(ollama_models)
        if complexity < 0.2:
            tier = Tier.LOCAL_OLLAMA if ollama_ok else Tier.FAST_MODEL
            if not ollama_ok:
                reasons.append("Ollama not reachable on localhost -> fast cloud model")
        elif complexity < 0.45:
            tier = Tier.FAST_MODEL
        elif complexity < 0.7:
            tier = Tier.BALANCED_MODEL
        else:
            tier = Tier.ARCHITECT_MODEL
        model = models[tier]
        if tier == Tier.LOCAL_OLLAMA and not model:
            model = (ollama_models or ["llama3.1"])[0]
        inp = estimate_tokens(prompt) + (self.context_tokens if context_tokens is None else context_tokens)
        out = int(300 + complexity * 3500)
        baseline_model = models[Tier.ARCHITECT_MODEL]
        return RoutingDecision(
            tier=tier,
            model=model,
            complexity=complexity,
            reasons=reasons or ["no strong signals; default tier"],
            est_input_tokens=inp,
            est_output_tokens=out,
            est_cost_usd=_cost(model, inp, out, tier),
            baseline_model=baseline_model,
            baseline_cost_usd=_cost(baseline_model, inp, out, Tier.ARCHITECT_MODEL),
            ollama_available=ollama_ok,
        )

    def route_many(self, prompts: list[str]) -> dict[str, Any]:
        decisions = [self.route(p) for p in prompts]
        total = sum(d.est_cost_usd for d in decisions)
        base = sum(d.baseline_cost_usd for d in decisions)
        return {
            "decisions": [d.to_dict() for d in decisions],
            "total_cost_usd": round(total, 6),
            "baseline_cost_usd": round(base, 6),
            "savings_usd": round(base - total, 6),
            "savings_pct": round((base - total) / base * 100, 1) if base else 0.0,
        }
