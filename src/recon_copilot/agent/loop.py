"""The agent loop, written by hand on the Messages API (no agent framework)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import anthropic
from anthropic.types.beta import BetaMessage
from pydantic import ValidationError

from recon_copilot.agent.budget import BudgetExceeded, TokenBudget
from recon_copilot.agent.llm import LLM
from recon_copilot.agent.prompts import SYSTEM_PROMPT, user_message
from recon_copilot.agent.tools import READ_TOOL_ALLOWLIST, ids_in, to_anthropic_tools
from recon_copilot.config import Settings
from recon_copilot.domain import Action, Proposal, ReconException, proposal_json_schema
from recon_copilot.mcp_client import LedgerClient
from recon_copilot.pricing import TokenUsage


class RunStatus(StrEnum):
    OK = "ok"
    REFUSED = "refused"  # model stop_reason == "refusal"
    TRUNCATED = "truncated"  # hit max_tokens
    TURN_LIMIT = "turn_limit"
    BUDGET_STOPPED = "budget_stopped"
    API_ERROR = "api_error"
    INVALID_OUTPUT = "invalid_output"


@dataclass
class CallRecord:
    turn: int
    served_model: str
    stop_reason: str | None
    usages: list[TokenUsage]
    latency_s: float
    count_latency_s: float
    request_id: str | None


@dataclass
class ToolCallRecord:
    turn: int
    name: str
    arguments: dict[str, Any]
    is_error: bool
    refused: bool  # not on the allowlist; never sent to the server


@dataclass
class AgentRun:
    exception_id: str
    status: RunStatus
    proposal: Proposal | None = None
    downgraded: bool = False  # evidence check replaced the action with escalate_to_human
    downgrade_reason: str = ""
    detail: str = ""
    calls: list[CallRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    latency_s: float = 0.0

    @property
    def usages(self) -> list[TokenUsage]:
        return [u for c in self.calls for u in c.usages]

    @property
    def refused_tool_calls(self) -> int:
        return sum(1 for t in self.tool_calls if t.refused)


def _usages(msg: BetaMessage) -> list[TokenUsage]:
    """Per-model usage. With a refusal fallback, `iterations` splits usage by model."""
    u = msg.usage
    iterations = [i for i in (u.iterations or []) if getattr(i, "model", None)]
    if iterations:
        return [
            TokenUsage(
                model=str(getattr(i, "model", "")),
                input_tokens=i.input_tokens,
                output_tokens=i.output_tokens,
                cache_creation_input_tokens=i.cache_creation_input_tokens or 0,
                cache_read_input_tokens=i.cache_read_input_tokens or 0,
            )
            for i in iterations
        ]
    return [
        TokenUsage(
            model=msg.model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
            cache_read_input_tokens=u.cache_read_input_tokens or 0,
        )
    ]


def _final_text(msg: BetaMessage) -> str | None:
    texts = [b.text for b in msg.content if b.type == "text"]
    return texts[-1] if texts else None


def evidence_check(proposal: Proposal, seen_ids: set[str]) -> tuple[Proposal, str]:
    """Downgrade to escalate_to_human if the proposal targets IDs the agent never looked up."""
    targets = set(proposal.target.ledger_entry_ids) | set(proposal.target.bank_record_ids)
    unseen = sorted(targets - seen_ids)
    if not unseen or proposal.action == Action.ESCALATE_TO_HUMAN:
        return proposal, ""
    reason = f"targets never returned by a tool: {', '.join(unseen)}"
    downgraded = proposal.model_copy(
        update={
            "action": Action.ESCALATE_TO_HUMAN,
            "rationale": f"[downgraded: {reason}] {proposal.rationale}",
        }
    )
    return downgraded, reason


class Agent:
    def __init__(self, llm: LLM, ledger: LedgerClient, settings: Settings) -> None:
        self._llm = llm
        self._ledger = ledger
        self._settings = settings
        self._tools: list[dict[str, Any]] | None = None

    async def tools(self) -> list[dict[str, Any]]:
        if self._tools is None:
            self._tools = to_anthropic_tools(await self._ledger.list_tools())
        return self._tools

    def _request(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "model": self._settings.model,
            "max_tokens": self._settings.max_tokens,
            "system": SYSTEM_PROMPT,
            "tools": tools,
            "messages": messages,
            "output_config": {
                "effort": self._settings.effort,
                "format": {"type": "json_schema", "schema": proposal_json_schema()},
            },
            "cache_control": {"type": "ephemeral"},
        }

    async def run(self, exc: ReconException, budget: TokenBudget) -> AgentRun:
        started = time.perf_counter()
        run = AgentRun(exception_id=exc.exception_id, status=RunStatus.TURN_LIMIT)
        try:
            await self._loop(exc, budget, run)
        finally:
            run.latency_s = time.perf_counter() - started
        return run

    async def _loop(self, exc: ReconException, budget: TokenBudget, run: AgentRun) -> None:
        tools = await self.tools()
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_message(exc.model_dump_json(indent=2))}
        ]
        seen_ids: set[str] = set()

        for turn in range(1, self._settings.max_turns + 1):
            request = self._request(messages, tools)
            t0 = time.perf_counter()
            try:
                counted = await self._llm.count_tokens(request)
                count_latency = time.perf_counter() - t0
                budget.check(counted + self._settings.max_tokens)
                t1 = time.perf_counter()
                msg = await self._llm.create(request)
            except BudgetExceeded as e:
                run.status, run.detail = RunStatus.BUDGET_STOPPED, str(e)
                return
            except anthropic.APIError as e:
                run.status, run.detail = RunStatus.API_ERROR, f"{type(e).__name__}: {e}"
                return

            usages = _usages(msg)
            budget.record(sum(u.total for u in usages))
            run.calls.append(
                CallRecord(
                    turn=turn,
                    served_model=msg.model,
                    stop_reason=msg.stop_reason,
                    usages=usages,
                    latency_s=time.perf_counter() - t1,
                    count_latency_s=count_latency,
                    request_id=getattr(msg, "_request_id", None),
                )
            )

            if msg.stop_reason == "refusal":
                category = msg.stop_details.category if msg.stop_details else None
                run.status, run.detail = RunStatus.REFUSED, f"category={category}"
                return
            if msg.stop_reason == "max_tokens":
                run.status = RunStatus.TRUNCATED
                return

            # Append the assistant turn unchanged (thinking blocks included).
            messages.append({"role": "assistant", "content": msg.content})

            if msg.stop_reason == "pause_turn":
                continue

            if msg.stop_reason == "tool_use":
                results = []
                for block in msg.content:
                    if block.type != "tool_use":
                        continue
                    results.append(
                        await self._run_tool(turn, block.id, block.name, block.input, seen_ids, run)
                    )
                messages.append({"role": "user", "content": results})
                continue

            # end_turn (or stop_sequence): the final, schema-constrained answer.
            self._finish(msg, seen_ids, run)
            return

        run.status, run.detail = (
            RunStatus.TURN_LIMIT,
            f"no answer after {self._settings.max_turns} turns",
        )

    async def _run_tool(
        self,
        turn: int,
        tool_use_id: str,
        name: str,
        raw_input: object,
        seen_ids: set[str],
        run: AgentRun,
    ) -> dict[str, Any]:
        args: dict[str, Any] = dict(raw_input) if isinstance(raw_input, dict) else {}
        if name not in READ_TOOL_ALLOWLIST:
            run.tool_calls.append(ToolCallRecord(turn, name, args, is_error=True, refused=True))
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": f"Tool {name!r} is not available. Only read-only tools can be used.",
                "is_error": True,
            }
        outcome = await self._ledger.call(name, args)
        run.tool_calls.append(ToolCallRecord(turn, name, args, outcome.is_error, refused=False))
        if not outcome.is_error and outcome.data is not None:
            seen_ids |= ids_in(outcome.data)
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": outcome.text,
            "is_error": outcome.is_error,
        }

    @staticmethod
    def _finish(msg: BetaMessage, seen_ids: set[str], run: AgentRun) -> None:
        text = _final_text(msg)
        if text is None:
            run.status, run.detail = RunStatus.INVALID_OUTPUT, "no text in final turn"
            return
        try:
            proposal = Proposal.model_validate(json.loads(text))
        except (ValueError, ValidationError) as e:
            run.status, run.detail = RunStatus.INVALID_OUTPUT, f"{type(e).__name__}: {e}"[:500]
            return
        checked, reason = evidence_check(proposal, seen_ids)
        run.proposal = checked
        run.downgraded = bool(reason)
        run.downgrade_reason = reason
        run.status = RunStatus.OK
