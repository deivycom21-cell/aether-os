"""BYOK LLM access for the swarm: Anthropic (official SDK, optional extra ``aether-os[llm]``) and
local Ollama. Credentials come only from the user's environment / local profiles."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from aether_swarm.router import OllamaProbe, RoutingDecision, Tier

# Models that support the server-side refusal fallback (Anthropic first-party API).
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1")


class LLMUnavailableError(RuntimeError):
    """Raised when no usable LLM backend (credentials, daemon, model) is available."""


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model, "provider": self.provider, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "stop_reason": self.stop_reason}


def anthropic_credentials_present() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if os.environ.get("ANTHROPIC_PROFILE"):
        return True
    return (Path.home() / ".config" / "anthropic").is_dir()


def anthropic_sdk_installed() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


class LLMClient:
    def __init__(self, ollama: OllamaProbe | None = None) -> None:
        self.ollama = ollama or OllamaProbe()

    def available(self) -> dict[str, bool]:
        return {
            "anthropic": anthropic_sdk_installed() and anthropic_credentials_present(),
            "ollama": self.ollama.available(),
        }

    def complete(self, decision: RoutingDecision, system: str, prompt: str, max_tokens: int = 16000) -> LLMResponse:
        if decision.tier == Tier.LOCAL_OLLAMA:
            return self._ollama(decision.model, system, prompt)
        return self._anthropic(decision.model, system, prompt, max_tokens)

    def _ollama(self, model: str, system: str, prompt: str) -> LLMResponse:
        if not self.ollama.available():
            raise LLMUnavailableError(f"Ollama is not reachable at {self.ollama.url}")
        r = httpx.post(
            f"{self.ollama.url}/api/chat",
            json={"model": model, "stream": False,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]},
            timeout=600,
        )
        r.raise_for_status()
        data = r.json()
        return LLMResponse(
            text=data.get("message", {}).get("content", ""),
            model=model,
            provider="ollama",
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            stop_reason=str(data.get("done_reason") or "stop"),
        )

    def _anthropic(self, model: str, system: str, prompt: str, max_tokens: int) -> LLMResponse:
        if not anthropic_sdk_installed():
            raise LLMUnavailableError("Install the optional extra: pip install 'aether-os[llm]'")
        import anthropic

        client = anthropic.Anthropic()
        messages = [{"role": "user", "content": prompt}]
        try:
            if model in _FALLBACK_MODELS:
                # Server-side refusal fallback: the API re-runs a declined request on a fallback model.
                response = client.beta.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                    betas=["server-side-fallback-2026-07-01"],
                    extra_body={"fallbacks": "default"},
                )
            else:
                response = client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=messages)
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailableError(f"Anthropic authentication failed: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailableError(f"Anthropic rate limit: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailableError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailableError(f"Cannot reach the Anthropic API: {exc}") from exc
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMUnavailableError(f"The model declined this request (category: {category or 'unspecified'}).")
        text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
        return LLMResponse(
            text=text,
            model=getattr(response, "model", model),
            provider="anthropic",
            input_tokens=int(response.usage.input_tokens or 0),
            output_tokens=int(response.usage.output_tokens or 0),
            stop_reason=str(response.stop_reason),
        )
