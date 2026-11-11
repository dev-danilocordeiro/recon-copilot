"""Cost from real usage numbers and a versioned price table (pricing.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

_DEFAULT_TABLE = Path(__file__).resolve().parents[2] / "pricing.toml"


@dataclass(frozen=True)
class TokenUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@cache
def load_table(path: Path = _DEFAULT_TABLE) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def cost_usd(usages: list[TokenUsage], table: dict[str, Any] | None = None) -> float | None:
    """Sum of costs, or None if any usage needs a rate the table doesn't have."""
    table = load_table() if table is None else table
    models: dict[str, dict[str, float]] = table.get("models", {})
    total = 0.0
    for u in usages:
        rates = models.get(u.model)
        if rates is None:
            return None
        prompt = u.input_tokens + u.cache_creation_input_tokens + u.cache_read_input_tokens
        if "max_prompt_tokens" in rates and prompt > rates["max_prompt_tokens"]:
            return None
        parts = [
            (u.input_tokens, "input"),
            (u.output_tokens, "output"),
            (u.cache_creation_input_tokens, "cache_write_5m"),
            (u.cache_read_input_tokens, "cache_read"),
        ]
        for tokens, key in parts:
            if tokens == 0:
                continue
            if key not in rates:
                return None
            total += tokens * rates[key] / 1_000_000
    return total
