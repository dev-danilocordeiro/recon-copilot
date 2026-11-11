"""The only place that talks to the Anthropic API.

The agent loop depends on the `LLM` protocol, so tests can script responses without a
network or an API key.
"""

from __future__ import annotations

from typing import Any, Protocol

import anthropic
from anthropic.types.beta import BetaMessage

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLM(Protocol):
    async def create(self, params: dict[str, Any]) -> BetaMessage: ...

    async def count_tokens(self, params: dict[str, Any]) -> int: ...


class AnthropicLLM:
    """Messages API through the official SDK (beta namespace, for `fallbacks`)."""

    def __init__(self, client: anthropic.AsyncAnthropic, *, fallbacks: bool) -> None:
        self._client = client
        self._fallbacks = fallbacks

    async def create(self, params: dict[str, Any]) -> BetaMessage:
        extra: dict[str, Any] = {}
        if self._fallbacks:
            extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        msg: BetaMessage = await self._client.beta.messages.create(**params, **extra)
        return msg

    async def count_tokens(self, params: dict[str, Any]) -> int:
        # count_tokens takes the same request minus generation-only fields.
        countable = {k: v for k, v in params.items() if k not in ("max_tokens",)}
        result = await self._client.beta.messages.count_tokens(**countable)
        return result.input_tokens
