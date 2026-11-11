"""Interactive human review of one proposal, then execution if (and only if) approved."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from recon_copilot.audit import AuditLog
from recon_copilot.domain import Proposal
from recon_copilot.executor import (
    ExecutionRefused,
    ExecutionResult,
    execute,
    grant_approval,
    plan_writes,
)
from recon_copilot.mcp_client import LedgerClient

Ask = Callable[[str], str]
Say = Callable[[str], None]


@dataclass
class ReviewOutcome:
    decision: str  # "approved" | "rejected" | "not_executable" | "tampered"
    result: ExecutionResult | None = None
    detail: str = ""


def load_proposal(record: dict[str, Any]) -> Proposal:
    """A run record written by `propose`. Refuses a record edited after it was written."""
    if record.get("proposal") is None:
        raise ExecutionRefused(f"no proposal in record (status={record.get('status')})")
    proposal = Proposal.model_validate(record["proposal"])
    if proposal.digest() != record.get("proposal_digest"):
        raise ExecutionRefused("proposal does not match its recorded digest; it was edited")
    return proposal


async def _show_current_records(proposal: Proposal, ledger: LedgerClient, say: Say) -> None:
    """Re-read every targeted record now, so the reviewer sees the ledger as it is."""
    t = proposal.target
    lookups = [("get_ledger_entry", "entry_id", i) for i in t.ledger_entry_ids] + [
        ("get_bank_record", "record_id", i) for i in t.bank_record_ids
    ]
    for tool, key, value in lookups:
        out = await ledger.call(tool, {key: value})
        body = out.text if out.is_error else json.dumps(out.data, indent=2, sort_keys=True)
        say(f"--- {value} (current) ---\n{body}")


async def review_and_execute(
    record: dict[str, Any],
    ledger: LedgerClient,
    audit: AuditLog,
    *,
    approver: str,
    ask: Ask,
    say: Say,
) -> ReviewOutcome:
    exception_id = str(record.get("exception_id", "?"))
    try:
        proposal = load_proposal(record)
    except ExecutionRefused as e:
        audit.append("review_refused", exception_id=exception_id, reason=str(e))
        return ReviewOutcome("tampered", detail=str(e))

    try:
        calls = plan_writes(proposal)
    except ExecutionRefused as e:
        audit.append("review_refused", exception_id=exception_id, reason=str(e))
        say(f"Nothing to execute: {e}")
        return ReviewOutcome("not_executable", detail=str(e))

    say(f"Exception {exception_id}")
    say(f"Cause:      {proposal.cause.value} (confidence: {proposal.confidence})")
    say(f"Action:     {proposal.action.value}")
    say(f"Rationale:  {proposal.rationale}")
    for ev in proposal.evidence:
        say(f"  evidence  [{ev.source_id}] {ev.fact}")
    if record.get("downgraded"):
        say(f"NOTE: downgraded by evidence check: {record.get('downgrade_reason')}")
    await _show_current_records(proposal, ledger, say)
    say("Planned ledger writes:")
    for c in calls:
        say(f"  {c.describe()}")
    if not calls:
        say("  (none: the exception is only marked)")

    audit.append(
        "proposal_shown",
        exception_id=exception_id,
        proposal_digest=proposal.digest(),
        planned=[c.describe() for c in calls],
        approver=approver,
    )
    answer = ask("Apply these changes? Type 'yes' to approve: ").strip().lower()
    if answer != "yes":
        audit.append("rejected", exception_id=exception_id, approver=approver, answer=answer)
        return ReviewOutcome("rejected")

    approval = grant_approval(proposal, approver=approver, exception_id=exception_id)
    audit.append(
        "approved",
        exception_id=exception_id,
        approver=approver,
        approval_id=approval.approval_id,
        proposal_digest=approval.proposal_digest,
    )
    result = await execute(proposal, approval, ledger, audit)
    for a in result.applied:
        say(f"applied: {a['call']}")
    if result.note:
        say(result.note)
    return ReviewOutcome("approved", result=result)
