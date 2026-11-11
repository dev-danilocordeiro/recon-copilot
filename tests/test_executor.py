"""The human approval gate. These tests are the evidence for 'never writes without approval'."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from datagen.scenarios import PlantedCase
from ledger_mcp_stub.ledger import Ledger
from ledger_mcp_stub.server import build_server
from recon_copilot.agent.budget import TokenBudget
from recon_copilot.agent.loop import Agent
from recon_copilot.approval import review_and_execute
from recon_copilot.audit import AuditLog
from recon_copilot.config import Settings
from recon_copilot.domain import Action, Cause, Dataset, Evidence, Proposal, ProposalTarget
from recon_copilot.executor import ExecutionRefused, execute, grant_approval, plan_writes
from recon_copilot.mcp_client import connect_in_process
from tests.conftest import FakeLLM, final, tool_use

SRC = Path(__file__).resolve().parents[1] / "src" / "recon_copilot"


def fee_proposal(case: PlantedCase, **target: Any) -> Proposal:
    exp = case.expected
    t = {
        "ledger_entry_ids": exp.ledger_entry_ids,
        "bank_record_ids": exp.bank_record_ids,
        "amount_cents": exp.amount_cents,
    } | target
    return Proposal(
        cause=Cause.BANK_FEE_DEDUCTED,
        action=Action.POST_ADJUSTMENT,
        target=ProposalTarget(**t),
        evidence=[Evidence(source_id=exp.bank_record_ids[0], fact="net of fee")],
        confidence="high",
        rationale="fee",
    )


def record_for(p: Proposal, exception_id: str = "EXC-0001") -> dict[str, Any]:
    return {
        "exception_id": exception_id,
        "status": "ok",
        "proposal": p.model_dump(mode="json"),
        "proposal_digest": p.digest(),
    }


async def test_approved_proposal_is_applied_once(
    fee_case: tuple[PlantedCase, Dataset], tmp_path: Path
) -> None:
    case, ds = fee_case
    ledger = Ledger(ds)
    audit = AuditLog(tmp_path / "audit.jsonl")
    p = fee_proposal(case)
    async with connect_in_process(build_server(ledger)) as client:
        approval = grant_approval(p, approver="Ana", exception_id="EXC-0001")
        result = await execute(p, approval, client, audit)
        assert len(result.applied) == 1
        assert len(ledger.writes) == 1
        new_id = ledger.writes[0]["new_entry_id"]
        assert ledger.entry(new_id).amount_cents == case.expected.amount_cents
        assert ledger.writes[0]["approval_id"] == approval.approval_id

        with pytest.raises(ExecutionRefused, match="already used"):
            await execute(p, approval, client, audit)
    assert len(ledger.writes) == 1


async def test_approval_for_another_proposal_is_refused(
    fee_case: tuple[PlantedCase, Dataset], tmp_path: Path
) -> None:
    case, ds = fee_case
    ledger = Ledger(ds)
    approved = fee_proposal(case)
    swapped = fee_proposal(case, amount_cents=-999_999)
    approval = grant_approval(approved, approver="Ana", exception_id="EXC-0001")
    async with connect_in_process(build_server(ledger)) as client:
        with pytest.raises(ExecutionRefused, match="does not match"):
            await execute(swapped, approval, client, AuditLog(tmp_path / "a.jsonl"))
    assert ledger.writes == []


def test_approval_needs_a_name(fee_case: tuple[PlantedCase, Dataset]) -> None:
    with pytest.raises(ExecutionRefused):
        grant_approval(fee_proposal(fee_case[0]), approver="  ", exception_id="E")


@pytest.mark.parametrize(
    ("action", "target", "tool"),
    [
        (Action.LINK_RECORDS, (["L-1"], ["B-1"], None), "link_records"),
        (Action.POST_ADJUSTMENT, (["L-1"], ["B-1"], -250), "post_adjustment"),
        (Action.REVERSE_DUPLICATE, (["L-2"], [], None), "reverse_entry"),
        (Action.CREATE_MISSING_ENTRY, ([], ["B-1"], 100), "create_entry_from_bank_record"),
    ],
)
def test_plan_writes_maps_actions(
    action: Action, target: tuple[list[str], list[str], int | None], tool: str
) -> None:
    p = Proposal(
        cause=Cause.UNKNOWN,
        action=action,
        target=ProposalTarget(
            ledger_entry_ids=target[0], bank_record_ids=target[1], amount_cents=target[2]
        ),
        evidence=[],
        confidence="high",
        rationale="",
    )
    [call] = plan_writes(p)
    assert call.tool == tool
    assert "approval_id" not in call.arguments  # added only at execution time


@pytest.mark.parametrize(
    ("action", "target"),
    [
        (Action.REVERSE_DUPLICATE, (["L-1", "L-2"], [], None)),  # ambiguous target
        (Action.POST_ADJUSTMENT, (["L-1"], ["B-1"], None)),  # no amount
        (Action.POST_ADJUSTMENT, (["L-1"], ["B-1"], 0)),
        (Action.LINK_RECORDS, (["L-1"], [], None)),
        (Action.ESCALATE_TO_HUMAN, ([], [], None)),
        (Action.WAIT_NEXT_FILE, (["L-1"], [], None)),
    ],
)
def test_plan_writes_refuses_bad_shapes(
    action: Action, target: tuple[list[str], list[str], int | None]
) -> None:
    p = Proposal(
        cause=Cause.UNKNOWN,
        action=action,
        target=ProposalTarget(
            ledger_entry_ids=target[0], bank_record_ids=target[1], amount_cents=target[2]
        ),
        evidence=[],
        confidence="low",
        rationale="",
    )
    with pytest.raises(ExecutionRefused):
        plan_writes(p)


@pytest.mark.parametrize("answer", ["", "n", "no", "y", "YES please", "sim"])
async def test_anything_but_yes_rejects(
    answer: str, fee_case: tuple[PlantedCase, Dataset], tmp_path: Path
) -> None:
    case, ds = fee_case
    ledger = Ledger(ds)
    audit = AuditLog(tmp_path / "audit.jsonl")
    async with connect_in_process(build_server(ledger)) as client:
        out = await review_and_execute(
            record_for(fee_proposal(case)),
            client,
            audit,
            approver="Ana",
            ask=lambda _: answer,
            say=lambda _: None,
        )
    assert out.decision == "rejected"
    assert ledger.writes == []
    assert [e["event"] for e in audit.events()] == ["proposal_shown", "rejected"]


async def test_yes_applies_and_audits(
    fee_case: tuple[PlantedCase, Dataset], tmp_path: Path
) -> None:
    case, ds = fee_case
    ledger = Ledger(ds)
    audit = AuditLog(tmp_path / "audit.jsonl")
    shown: list[str] = []
    async with connect_in_process(build_server(ledger)) as client:
        out = await review_and_execute(
            record_for(fee_proposal(case)),
            client,
            audit,
            approver="Ana",
            ask=lambda _: "yes",
            say=shown.append,
        )
    assert out.decision == "approved"
    assert len(ledger.writes) == 1
    events = [e["event"] for e in audit.events()]
    assert events == ["proposal_shown", "approved", "write_applied"]
    # The reviewer saw the current ledger records and the planned call.
    assert any(case.expected.ledger_entry_ids[0] in line for line in shown)
    assert any("post_adjustment(" in line for line in shown)


async def test_edited_record_is_refused(
    fee_case: tuple[PlantedCase, Dataset], tmp_path: Path
) -> None:
    case, ds = fee_case
    ledger = Ledger(ds)
    record = record_for(fee_proposal(case))
    record["proposal"]["target"]["amount_cents"] = -1_000_000  # edited after `propose`
    async with connect_in_process(build_server(ledger)) as client:
        out = await review_and_execute(
            record,
            client,
            AuditLog(tmp_path / "a.jsonl"),
            approver="Ana",
            ask=lambda _: "yes",
            say=lambda _: None,
        )
    assert out.decision == "tampered"
    assert ledger.writes == []


async def test_agent_cannot_write_even_if_it_tries(
    fee_case: tuple[PlantedCase, Dataset], settings: Settings
) -> None:
    case, ds = fee_case
    ledger = Ledger(ds)
    eid = case.expected.ledger_entry_ids[0]
    rid = case.expected.bank_record_ids[0]
    attempts = [
        tool_use("reverse_entry", {"entry_id": eid, "reason": "x", "approval_id": "A"}, "1"),
        tool_use(
            "post_adjustment",
            {"entry_id": eid, "amount_cents": 1, "reason": "x", "approval_id": "A"},
            "2",
        ),
        tool_use("link_records", {"entry_id": eid, "record_id": rid, "approval_id": "A"}, "3"),
        tool_use(
            "create_entry_from_bank_record",
            {"record_id": rid, "reason": "x", "approval_id": "A"},
            "4",
        ),
    ]
    escalate = {
        "cause": "unknown",
        "action": "escalate_to_human",
        "target": {"ledger_entry_ids": [], "bank_record_ids": [], "amount_cents": None},
        "evidence": [],
        "confidence": "low",
        "rationale": "",
    }
    async with connect_in_process(build_server(ledger)) as client:
        run = await Agent(FakeLLM([*attempts, final(escalate)]), client, settings).run(
            case.exception, TokenBudget(None)
        )
    assert run.refused_tool_calls == 4
    assert ledger.writes == []


def test_agent_package_does_not_import_the_executor() -> None:
    forbidden = {"recon_copilot.executor", "recon_copilot.approval"}
    for path in (SRC / "agent").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not set(names) & forbidden, f"{path.name} imports {names}"
