"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from typing import Literal

Effort = Literal["low", "medium", "high", "xhigh", "max"]
_EFFORTS: tuple[Effort, ...] = ("low", "medium", "high", "xhigh", "max")

DEFAULT_MODEL = "claude-opus-5-5"

# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
# Others get no fallback; the model that actually answered is always recorded.
FALLBACK_CAPABLE_MODELS = frozenset(
    {
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-sonnet-5-5",
        "claude-fable-5-1",
        "claude-fable-5-2",
    }
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    model: str
    effort: Effort
    max_tokens: int
    max_turns: int
    fallbacks: bool
    eval_token_ceiling: int | None
    ledger_mcp_command: list[str] | None  # None → spawn the local stub

    def stub_command(self, dataset_path: str) -> list[str]:
        """Command for the ledger-mcp server, given the dataset the stub should serve."""
        if self.ledger_mcp_command is not None:
            return self.ledger_mcp_command
        return [sys.executable, "-m", "ledger_mcp_stub", "--dataset", dataset_path]


def _int_env(env: dict[str, str], name: str, default: int | None) -> int | None:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ) if env is None else env

    model = env.get("RECON_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    effort_raw = env.get("RECON_EFFORT", "medium").strip()
    if effort_raw not in _EFFORTS:
        raise ConfigError(f"RECON_EFFORT must be one of {_EFFORTS}, got {effort_raw!r}")
    effort: Effort = effort_raw

    fallbacks_raw = env.get("RECON_FALLBACKS", "auto").strip()
    if fallbacks_raw not in ("auto", "off"):
        raise ConfigError(f"RECON_FALLBACKS must be 'auto' or 'off', got {fallbacks_raw!r}")
    fallbacks = fallbacks_raw == "auto" and model in FALLBACK_CAPABLE_MODELS

    command_raw = env.get("LEDGER_MCP_COMMAND", "").strip()

    max_tokens = _int_env(env, "RECON_MAX_TOKENS", 16000)
    max_turns = _int_env(env, "RECON_MAX_TURNS", 8)
    assert max_tokens is not None and max_turns is not None

    return Settings(
        model=model,
        effort=effort,
        max_tokens=max_tokens,
        max_turns=max_turns,
        fallbacks=fallbacks,
        eval_token_ceiling=_int_env(env, "RECON_EVAL_TOKEN_CEILING", None),
        ledger_mcp_command=shlex.split(command_raw) if command_raw else None,
    )
