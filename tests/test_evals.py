"""Eval machinery without the API: graders, runner, ceiling, report, regression gate."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from datagen.scenarios import Expected
from evals.baseline import check, compute_baseline
from evals.graders import grade
from evals.run import GoldenCase, load_golden, run_eval, summarize, to_markdown
from ledger_mcp_stub.ledger import Ledger
from ledger_mcp_stub.server import build_server, load_dataset
from recon_copilot.config import Settings
from recon_copilot.domain import Action, Cause
from recon_copilot.mcp_client import LedgerClient, connect_in_process
from tests.conftest import FakeLLM, final, tool_use


def expected(**kw: Any) -> Expected:
    base: dict[str, Any] = {
        "cause": Cause.BANK_FEE_DEDUCTED,
        "accepted_causes": [Cause.BANK_FEE_DEDUCTED],
        "accepted_actions": [Action.POST_ADJUSTMENT],
        "ledger_entry_ids": ["L-1"],
        "bank_record_ids": ["B-1"],
        "amount_cents": -250,
    }
    return Expected(**(base | kw))


def record(
    cause: str, action: str, ledger: list[str], bank: list[str], amount: int | None, **extra: Any
) -> dict[str, Any]:
    return {
        "status": "ok",
        "proposal": {
            "cause": cause,
            "action": action,
            "target": {"ledger_entry_ids": ledger, "bank_record_ids": bank, "amount_cents": amount},
            "evidence": [],
            "confidence": "high",
            "rationale": "",
        },
        **extra,
    }


# ---- graders ---------------------------------------------------------------------------


def test_grade_correct() -> None:
    g = grade(expected(), record("bank_fee_deducted", "post_adjustment", ["L-1"], ["B-1"], -250))
    assert g.correct and g.safe


@pytest.mark.parametrize(
    ("rec", "why"),
    [
        (record("bank_fee_deducted", "post_adjustment", ["L-1"], ["B-1"], 250), "amount"),
        (record("bank_fee_deducted", "post_adjustment", ["L-2"], ["B-1"], -250), "ledger"),
        (record("bank_fee_deducted", "link_records", ["L-1"], ["B-1"], None), "not in"),
    ],
)
def test_grade_action_wrong(rec: dict[str, Any], why: str) -> None:
    g = grade(expected(), rec)
    assert g.classification and not g.action and not g.correct
    assert why in g.action_detail


def test_wrong_cause_right_action_is_not_correct() -> None:
    g = grade(expected(), record("partial_payment", "post_adjustment", ["L-1"], ["B-1"], -250))
    assert not g.classification and g.action and not g.correct


def test_escalate_needs_no_targets() -> None:
    exp = expected(
        cause=Cause.UNKNOWN,
        accepted_causes=[Cause.UNKNOWN],
        accepted_actions=[Action.ESCALATE_TO_HUMAN],
        amount_cents=None,
        unsafe_actions=[Action.POST_ADJUSTMENT],
    )
    assert grade(exp, record("unknown", "escalate_to_human", [], [], None)).correct
    bad = grade(exp, record("unknown", "post_adjustment", ["L-1"], ["B-1"], -1))
    assert not bad.correct
    assert any("unsafe" in f for f in bad.safety_failures)
    assert any("none is correct" in f for f in bad.safety_failures)


def test_safety_flags_refused_tools_and_downgrades() -> None:
    rec = record(
        "bank_fee_deducted",
        "post_adjustment",
        ["L-1"],
        ["B-1"],
        -250,
        refused_tool_calls=1,
        downgraded=True,
        downgrade_reason="x",
    )
    g = grade(expected(), rec)
    assert g.correct  # accuracy and safety are separate
    assert len(g.safety_failures) == 2


def test_non_answer() -> None:
    g = grade(expected(), {"status": "truncated", "proposal": None})
    assert not g.answered and g.non_answer == "truncated" and not g.correct


# ---- runner ----------------------------------------------------------------------------


@asynccontextmanager
async def in_process(gc: GoldenCase) -> AsyncIterator[LedgerClient]:
    async with connect_in_process(build_server(Ledger(load_dataset(gc.dataset_path)))) as c:
        yield c


def scripted_answer(gc: GoldenCase) -> list[Any]:
    """Look up every expected ID, then answer exactly what the key expects."""
    exp = gc.case.expected
    calls = [tool_use("get_ledger_entry", {"entry_id": i}, f"l{i}") for i in exp.ledger_entry_ids]
    calls += [tool_use("get_bank_record", {"record_id": i}, f"b{i}") for i in exp.bank_record_ids]
    action = exp.accepted_actions[0]
    answer: dict[str, Any] = {
        "cause": exp.cause.value,
        "action": action.value,
        "target": {
            "ledger_entry_ids": exp.ledger_entry_ids,
            "bank_record_ids": exp.bank_record_ids,
            "amount_cents": exp.amount_cents,
        },
        "evidence": [],
        "confidence": "high",
        "rationale": "scripted",
    }
    return [*calls, final(answer)]


async def test_perfect_answers_score_40_of_40(settings: Settings) -> None:
    cases = load_golden()
    assert len(cases) == 40
    responses = [m for gc in cases for m in scripted_answer(gc)]
    report = await run_eval(cases, settings, FakeLLM(responses), in_process, None)
    s = report["summary"]
    assert s["complete"] and s["ran"] == 40
    assert s["correct"] == 40, [r for r in report["cases"] if not r["correct"]]
    assert s["safety_failures"] == 0
    assert check(report, {"status": "pending", "max_safety_failures": 0}) == []
    md = to_markdown(report)
    assert "40/40 (100.0%)" in md
    assert md.count("\n| G0") == 40


async def test_ceiling_stops_run_and_marks_rest_not_run(settings: Settings) -> None:
    cases = load_golden(only=["G001", "G002", "G003"])
    responses = [m for gc in cases for m in scripted_answer(gc)]
    # Each call reserves up to count (1000) + max_tokens (4000) and spends 1200, so a
    # 9000 ceiling allows a few calls and then must stop before running all three cases.
    report = await run_eval(cases, settings, FakeLLM(responses), in_process, 9000)
    s = report["summary"]
    assert not s["complete"]
    assert s["not_run"] >= 1
    assert report["meta"]["tokens_spent"] <= 9000
    assert "INCOMPLETE" in to_markdown(report)
    problems = check(report, {"status": "pending", "max_safety_failures": 0})
    assert any("incomplete" in p for p in problems)


# ---- regression gate -------------------------------------------------------------------


def fake_report(correct: int, safety: int = 0, model: str = "claude-opus-5-5") -> dict[str, Any]:
    rows = [
        {
            "group": "cause",
            "status": "ok",
            "correct": i < correct,
            "classification": True,
            "action": i < correct,
            "got_action": "x",
            "safety_failures": ["s"] if i < safety else [],
            "cost_usd": 0.01,
            "latency_s": 1.0,
            "tokens": {"input": 1, "output": 1, "cache_write": 0, "cache_read": 0},
        }
        for i in range(40)
    ]
    return {"meta": {"model": model, "effort": "medium", "at": "t"}, "summary": summarize(rows)}


def test_baseline_threshold_and_regression() -> None:
    runs = [fake_report(33), fake_report(35), fake_report(34)]
    b = compute_baseline(runs, ["a", "b", "c"])
    assert b["observed_correct"] == [33, 35, 34]
    assert b["min_correct"] == 33 - 2  # worst run minus spread
    assert check(fake_report(31), b) == []
    assert any("regressed" in p for p in check(fake_report(30), b))
    assert any("safety" in p for p in check(fake_report(35, safety=1), b))
    assert any("baseline model" in p for p in check(fake_report(35, model="other"), b))


def test_baseline_needs_two_runs_of_one_config() -> None:
    with pytest.raises(ValueError):
        compute_baseline([fake_report(30)], ["a"])
    with pytest.raises(ValueError):
        compute_baseline([fake_report(30), fake_report(30, model="x")], ["a", "b"])


def test_zero_spread_still_allows_one_case() -> None:
    b = compute_baseline([fake_report(30), fake_report(30)], ["a", "b"])
    assert b["min_correct"] == 29


def test_safety_gate_applies_before_any_baseline() -> None:
    problems = check(fake_report(40, safety=1), {"status": "pending", "max_safety_failures": 0})
    assert any("safety" in p for p in problems)
