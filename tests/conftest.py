from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from anthropic.types.beta import BetaMessage

from datagen.scenarios import PlantedCase, build_case
from ledger_mcp_stub.ledger import Ledger
from ledger_mcp_stub.server import build_server
from recon_copilot.config import Settings
from recon_copilot.domain import Dataset
from recon_copilot.mcp_client import LedgerClient, connect_in_process


def message(
    content: list[dict[str, Any]],
    stop_reason: str,
    *,
    input_tokens: int = 1000,
    output_tokens: int = 200,
    stop_details: dict[str, Any] | None = None,
) -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "stop_details": stop_details,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    )


def tool_use(name: str, args: dict[str, Any], id_: str = "tu_1") -> BetaMessage:
    return message([{"type": "tool_use", "id": id_, "name": name, "input": args}], "tool_use")


def final(proposal: dict[str, Any]) -> BetaMessage:
    return message([{"type": "text", "text": json.dumps(proposal)}], "end_turn")


class FakeLLM:
    """Returns scripted responses in order and records every request."""

    def __init__(self, responses: list[BetaMessage], count: int = 1000) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.count = count

    async def create(self, params: dict[str, Any]) -> BetaMessage:
        # Snapshot: the loop keeps appending to the same messages list.
        self.requests.append({**params, "messages": list(params["messages"])})
        return self.responses.pop(0)

    async def count_tokens(self, params: dict[str, Any]) -> int:
        return self.count


@pytest.fixture
def settings() -> Settings:
    return Settings(
        model="claude-opus-5-5",
        effort="medium",
        max_tokens=4000,
        max_turns=6,
        fallbacks=False,
        eval_token_ceiling=None,
        ledger_mcp_command=None,
    )


@pytest.fixture
def fee_case() -> tuple[PlantedCase, Dataset]:
    return build_case("bank_fee_deducted", seed=42, case_id="T001")


@asynccontextmanager
async def stub_client(ds: Dataset) -> AsyncIterator[LedgerClient]:
    """In-process ledger-mcp stub. Used inside each test (not as an async fixture: the MCP
    client's cancel scope must be entered and exited in the same task)."""
    async with connect_in_process(build_server(Ledger(ds))) as client:
        yield client
