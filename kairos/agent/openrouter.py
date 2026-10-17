"""Minimal OpenRouter client (OpenAI-compatible chat completions + tool calling) over httpx.

Configure in .env:
  OPENROUTER_API_KEY=sk-or-...
  OPENROUTER_MODEL=<a model id from https://openrouter.ai/models that supports tools>
  OPENROUTER_EXTRACT_MODEL=<optional cheaper model for notification extraction>
  OPENROUTER_FALLBACK_MODELS=<optional comma list; OpenRouter tries them in order if the first fails>

Any network/API failure raises LLMUnavailable and opens a short circuit breaker, so the agent drops to
the offline rule planner immediately instead of waiting on timeouts every turn.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from kairos.net import ssl_context

from kairos.config import Settings

log = logging.getLogger(__name__)

COOLDOWN_SECONDS = 60
AUTH_COOLDOWN_SECONDS = 300


class LLMUnavailable(RuntimeError):
    pass


class OpenRouterClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.s = settings
        self.model = settings.openrouter_model
        self.down_until = 0.0
        self.last_error: str | None = None
        self.http = httpx.Client(base_url=settings.openrouter_base_url, timeout=settings.llm_timeout,
                                 verify=ssl_context(),
                                 transport=transport)

    @property
    def configured(self) -> bool:
        return bool(self.s.openrouter_api_key and self.model)

    def available(self) -> bool:
        return self.configured and time.time() >= self.down_until

    def status(self) -> dict:
        if not self.configured:
            missing = "OPENROUTER_API_KEY" if not self.s.openrouter_api_key else "OPENROUTER_MODEL"
            return {"online": False, "model": self.model, "reason": f"{missing} not set"}
        if not self.available():
            return {"online": False, "model": self.model, "reason": self.last_error,
                    "retry_in_s": int(self.down_until - time.time())}
        return {"online": True, "model": self.model}

    def _trip(self, reason: str, seconds: int = COOLDOWN_SECONDS) -> LLMUnavailable:
        self.last_error = reason
        self.down_until = time.time() + seconds
        log.warning("OpenRouter unavailable (%s); using offline rules for %ss", reason, seconds)
        return LLMUnavailable(reason)

    def chat(self, messages: list[dict], *, model: str | None = None, tools: list[dict] | None = None,
             max_tokens: int = 700, temperature: float = 0.2, timeout: float | None = None) -> dict[str, Any]:
        """Returns the assistant message dict ({"content": ..., "tool_calls": [...]})."""
        if not self.configured:
            raise LLMUnavailable("OpenRouter not configured")
        if not self.available():
            raise LLMUnavailable(f"circuit open: {self.last_error}")
        model = model or self.model
        payload: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens,
                                   "temperature": temperature}
        if self.s.openrouter_fallback_models:
            payload["models"] = [model, *self.s.openrouter_fallback_models]
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.s.openrouter_api_key}",
                   "HTTP-Referer": "https://github.com/kairos-graphhacks", "X-Title": "KAIROS"}
        try:
            r = self.http.post("/chat/completions", json=payload, headers=headers,
                               timeout=timeout or self.s.llm_timeout)
        except httpx.HTTPError as exc:  # offline, DNS, timeout
            raise self._trip(f"network: {type(exc).__name__}") from exc
        if r.status_code >= 400:
            seconds = AUTH_COOLDOWN_SECONDS if r.status_code in (401, 402, 403) else COOLDOWN_SECONDS
            raise self._trip(f"HTTP {r.status_code}: {r.text[:200]}", seconds)
        try:
            data = r.json()
        except ValueError as exc:
            raise self._trip("invalid JSON from OpenRouter") from exc
        if data.get("error"):
            raise self._trip(f"API error: {str(data['error'])[:200]}")
        choices = data.get("choices") or []
        if not choices:
            raise self._trip("empty response")
        self.last_error = None
        return choices[0].get("message") or {}
