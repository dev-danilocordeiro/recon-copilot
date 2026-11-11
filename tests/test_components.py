from __future__ import annotations

import pytest

from datagen.scenarios import FEES, GOLDEN_PLAN, SCENARIOS, PlantedCase, build_case
from ledger_mcp_stub.ledger import Ledger
from recon_copilot.agent.budget import BudgetExceeded, TokenBudget
from recon_copilot.agent.tools import READ_TOOL_ALLOWLIST, ids_in, to_anthropic_tools
from recon_copilot.config import ConfigError, load_settings
from recon_copilot.domain import Action, Dataset
from recon_copilot.mcp_client import LedgerClient
from recon_copilot.pricing import TokenUsage, cost_usd
from tests.conftest import stub_client

# ---- datagen ---------------------------------------------------------------------------


def test_golden_plan_has_40_cases() -> None:
    assert sum(n for _, n in GOLDEN_PLAN) == 40


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_every_scenario_is_consistent(scenario: str) -> None:
    case, ds = build_case(scenario, seed=7, case_id="X")
    entry_ids = {e.entry_id for e in ds.ledger_entries}
    record_ids = {r.record_id for r in ds.bank_records}
    exp = case.expected
    assert set(case.exception.ledger_entry_ids) <= entry_ids
    assert set(case.exception.bank_record_ids) <= record_ids
    assert set(exp.ledger_entry_ids) <= entry_ids
    assert set(exp.bank_record_ids) <= record_ids
    assert exp.cause in exp.accepted_causes
    assert not set(exp.accepted_actions) & set(exp.unsafe_actions)


def test_generation_is_deterministic() -> None:
    a = build_case("wrong_reference", seed=3, case_id="X")
    b = build_case("wrong_reference", seed=3, case_id="X")
    assert a[0] == b[0]
    assert a[1].model_dump() == b[1].model_dump()


def test_fee_case_matches_schedule() -> None:
    case, ds = build_case("bank_fee_deducted", seed=11, case_id="X")
    entry = next(e for e in ds.ledger_entries if e.entry_id in case.expected.ledger_entry_ids)
    record = next(r for r in ds.bank_records if r.record_id in case.expected.bank_record_ids)
    fee = FEES[(entry.bank_code, entry.product)]
    assert entry.amount_cents - record.amount_cents == fee
    assert case.expected.amount_cents == record.amount_cents - entry.amount_cents


def test_injection_text_is_planted() -> None:
    case, ds = build_case("injection_fee_memo", seed=5, case_id="X")
    entry = next(e for e in ds.ledger_entries if e.entry_id == case.exception.ledger_entry_ids[0])
    assert "OVERRIDE" in entry.memo
    assert Action.REVERSE_DUPLICATE in case.expected.unsafe_actions


# ---- ledger stub -----------------------------------------------------------------------


def test_ledger_writes_need_approval(fee_case: tuple[PlantedCase, Dataset]) -> None:
    case, ds = fee_case
    ledger = Ledger(ds)
    entry_id = case.expected.ledger_entry_ids[0]
    with pytest.raises(PermissionError):
        ledger.reverse_entry(entry_id, "test", approval_id="")
    ledger.reverse_entry(entry_id, "test", approval_id="APR-1")
    assert ledger.entry(entry_id).status == "reversed"


def test_search_is_capped_and_sorted(fee_case: tuple[PlantedCase, Dataset]) -> None:
    ledger = Ledger(fee_case[1])
    res = ledger.search_ledger_entries(limit=1000)
    assert res["returned"] == 25
    dates = [e["posted_on"] for e in res["entries"]]
    assert dates == sorted(dates)


async def test_stub_over_mcp_and_allowlist(fee_case: tuple[PlantedCase, Dataset]) -> None:
    async with stub_client(fee_case[1]) as ledger_client:
        await _check_stub(ledger_client)


async def _check_stub(ledger_client: LedgerClient) -> None:
    tools = await ledger_client.list_tools()
    names = {t.name for t in tools}
    assert "reverse_entry" in names  # the server exposes writes...
    converted = to_anthropic_tools(tools)
    assert {t["name"] for t in converted} == READ_TOOL_ALLOWLIST  # ...the agent never sees them
    assert [t["name"] for t in converted] == sorted(t["name"] for t in converted)

    missing = await ledger_client.call("get_bank_record", {"record_id": "B-99999"})
    assert missing.is_error
    assert "not found" in missing.text


# ---- small units -----------------------------------------------------------------------


def test_ids_in_walks_nested_results() -> None:
    data = {"entries": [{"entry_id": "L-1", "x": {"record_id": "B-2"}}], "record_id": 3}
    assert ids_in(data) == {"L-1", "B-2"}


def test_budget() -> None:
    b = TokenBudget(ceiling=100)
    b.check(100)
    b.record(60)
    with pytest.raises(BudgetExceeded):
        b.check(41)
    assert TokenBudget(None).remaining is None


def test_settings_from_env() -> None:
    s = load_settings({"RECON_MODEL": "claude-haiku-5-5", "RECON_EVAL_TOKEN_CEILING": "500000"})
    assert s.model == "claude-haiku-5-5"
    assert s.fallbacks is False  # Haiku 5.5 has no server-side fallback
    assert s.eval_token_ceiling == 500_000
    assert load_settings({}).fallbacks is True
    with pytest.raises(ConfigError):
        load_settings({"RECON_EFFORT": "turbo"})
    with pytest.raises(ConfigError):
        load_settings({"RECON_EVAL_TOKEN_CEILING": "-1"})


def test_cost() -> None:
    u = TokenUsage("claude-opus-5-5", 1_000_000, 100_000, 0, 1_000_000)
    assert cost_usd([u]) == pytest.approx(4.00 + 2.00 + 0.20)
    assert cost_usd([TokenUsage("claude-unknown", 1, 1, 0, 0)]) is None
    # Haiku cache-read rate is not in the table: cost is unknown, not guessed.
    assert cost_usd([TokenUsage("claude-haiku-5-5", 10, 10, 0, 10)]) is None
