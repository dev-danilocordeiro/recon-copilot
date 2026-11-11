from __future__ import annotations

from typing import Any

from datagen.scenarios import PlantedCase
from recon_copilot.agent.budget import TokenBudget
from recon_copilot.agent.loop import Agent, AgentRun, RunStatus
from recon_copilot.agent.tools import READ_TOOL_ALLOWLIST
from recon_copilot.config import Settings
from recon_copilot.domain import Action, Cause, Dataset
from tests.conftest import FakeLLM, final, message, stub_client, tool_use


async def run_agent(
    llm: FakeLLM,
    fee_case: tuple[PlantedCase, Dataset],
    settings: Settings,
    budget: TokenBudget,
) -> AgentRun:
    case, ds = fee_case
    async with stub_client(ds) as client:
        return await Agent(llm, client, settings).run(case.exception, budget)


def fee_proposal(case: PlantedCase, amount: int | None = None) -> dict[str, Any]:
    exp = case.expected
    return {
        "cause": "bank_fee_deducted",
        "action": "post_adjustment",
        "target": {
            "ledger_entry_ids": exp.ledger_entry_ids,
            "bank_record_ids": exp.bank_record_ids,
            "amount_cents": exp.amount_cents if amount is None else amount,
        },
        "evidence": [{"source_id": exp.bank_record_ids[0], "fact": "net of fee"}],
        "confidence": "high",
        "rationale": "fee matches schedule",
    }


async def test_happy_path(fee_case: tuple[PlantedCase, Dataset], settings: Settings) -> None:
    case, _ = fee_case
    llm = FakeLLM(
        [
            tool_use("get_ledger_entry", {"entry_id": case.expected.ledger_entry_ids[0]}, "a"),
            tool_use("get_bank_record", {"record_id": case.expected.bank_record_ids[0]}, "b"),
            final(fee_proposal(case)),
        ]
    )
    run = await run_agent(llm, fee_case, settings, TokenBudget(None))

    assert run.status == RunStatus.OK
    assert run.proposal is not None
    assert run.proposal.cause == Cause.BANK_FEE_DEDUCTED
    assert run.proposal.action == Action.POST_ADJUSTMENT
    assert not run.downgraded
    assert len(run.calls) == 3
    # The request carries only read tools and the structured output schema.
    sent_tools = {t["name"] for t in llm.requests[0]["tools"]}
    assert sent_tools == READ_TOOL_ALLOWLIST
    assert llm.requests[0]["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in llm.requests[0]
    # Tool results come back in one user message after the assistant turn.
    second = llm.requests[1]["messages"]
    assert second[-1]["role"] == "user"
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert second[-1]["content"][0]["is_error"] is False


async def test_write_tool_is_refused_and_never_reaches_server(
    fee_case: tuple[PlantedCase, Dataset], settings: Settings
) -> None:
    case, _ = fee_case
    llm = FakeLLM(
        [
            tool_use(
                "reverse_entry",
                {"entry_id": case.expected.ledger_entry_ids[0], "reason": "x", "approval_id": "y"},
            ),
            final(fee_proposal(case)),
        ]
    )
    run = await run_agent(llm, fee_case, settings, TokenBudget(None))

    assert run.refused_tool_calls == 1
    result = llm.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "not available" in result["content"]
    # Nothing was looked up, so a write proposal is downgraded by the evidence check.
    assert run.downgraded
    assert run.proposal is not None
    assert run.proposal.action == Action.ESCALATE_TO_HUMAN


async def test_unseen_target_ids_downgrade_to_escalate(
    fee_case: tuple[PlantedCase, Dataset], settings: Settings
) -> None:
    case, _ = fee_case
    llm = FakeLLM(
        [
            tool_use("get_ledger_entry", {"entry_id": case.expected.ledger_entry_ids[0]}),
            final(fee_proposal(case)),  # bank record was never looked up
        ]
    )
    run = await run_agent(llm, fee_case, settings, TokenBudget(None))
    assert run.downgraded
    assert case.expected.bank_record_ids[0] in run.downgrade_reason
    assert run.proposal is not None
    assert run.proposal.action == Action.ESCALATE_TO_HUMAN
    assert run.proposal.cause == Cause.BANK_FEE_DEDUCTED  # cause is kept


async def test_tool_error_is_returned_to_model(
    fee_case: tuple[PlantedCase, Dataset], settings: Settings
) -> None:
    case, _ = fee_case
    llm = FakeLLM(
        [
            tool_use("get_ledger_entry", {"entry_id": "L-99999"}),
            final(fee_proposal(case) | {"action": "escalate_to_human"}),
        ]
    )
    run = await run_agent(llm, fee_case, settings, TokenBudget(None))
    result = llm.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "not found" in result["content"]
    assert run.status == RunStatus.OK


async def test_budget_stops_before_the_call(
    fee_case: tuple[PlantedCase, Dataset], settings: Settings
) -> None:
    case, _ = fee_case
    llm = FakeLLM([tool_use("list_return_files", {}), final(fee_proposal(case))], count=1000)
    # First call needs up to 1000 + 4000; it fits. It spends 1200, leaving 3800 < 5000.
    budget = TokenBudget(ceiling=6000)
    run = await run_agent(llm, fee_case, settings, budget)
    assert run.status == RunStatus.BUDGET_STOPPED
    assert len(llm.requests) == 1
    assert budget.spent == 1200
    assert budget.spent <= 6000


async def test_refusal_and_truncation(
    fee_case: tuple[PlantedCase, Dataset], settings: Settings
) -> None:
    refused = message(
        [], "refusal", stop_details={"type": "refusal", "category": None, "explanation": None}
    )
    run = await run_agent(FakeLLM([refused]), fee_case, settings, TokenBudget(None))
    assert run.status == RunStatus.REFUSED

    truncated = message([{"type": "text", "text": "{"}], "max_tokens")
    run = await run_agent(FakeLLM([truncated]), fee_case, settings, TokenBudget(None))
    assert run.status == RunStatus.TRUNCATED


async def test_invalid_output_and_turn_limit(
    fee_case: tuple[PlantedCase, Dataset], settings: Settings
) -> None:
    bad = message([{"type": "text", "text": '{"cause": "nope"}'}], "end_turn")
    run = await run_agent(FakeLLM([bad]), fee_case, settings, TokenBudget(None))
    assert run.status == RunStatus.INVALID_OUTPUT

    loops = [tool_use("list_return_files", {}, f"t{i}") for i in range(settings.max_turns)]
    run = await run_agent(FakeLLM(loops), fee_case, settings, TokenBudget(None))
    assert run.status == RunStatus.TURN_LIMIT
    assert len(run.calls) == settings.max_turns
