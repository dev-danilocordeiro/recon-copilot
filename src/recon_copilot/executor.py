"""Applies an approved proposal to the ledger. The only code that calls ledger write tools.

Guarantees, each covered by tests (tests/test_executor.py):
- Nothing runs without an Approval whose digest matches the exact proposal being applied.
- An approval is used once; replaying it is refused (checked against the audit log).
- Only write actions with a well-formed target can be planned; anything else is refused
  before any call is made.
- The agent package never imports this module.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from recon_copilot.audit import AuditLog
from recon_copilot.domain import Action, Proposal
from recon_copilot.mcp_client import LedgerClient


class ExecutionRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class Approval:
    approval_id: str
    approver: str
    approved_at: datetime
    exception_id: str
    proposal_digest: str


def grant_approval(proposal: Proposal, *, approver: str, exception_id: str) -> Approval:
    """Record a human's yes for exactly this proposal. Called by the interactive
    `approve` command after the person has seen the proposal and the planned writes."""
    if not approver.strip():
        raise ExecutionRefused("an approval needs the approver's name")
    return Approval(
        approval_id=f"APR-{secrets.token_hex(6)}",
        approver=approver.strip(),
        approved_at=datetime.now(UTC),
        exception_id=exception_id,
        proposal_digest=proposal.digest(),
    )


@dataclass(frozen=True)
class WriteCall:
    tool: str
    arguments: dict[str, Any]

    def describe(self) -> str:
        args = ", ".join(f"{k}={v!r}" for k, v in self.arguments.items())
        return f"{self.tool}({args})"


@dataclass
class ExecutionResult:
    approval_id: str
    applied: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""


def _one(ids: list[str], what: str, action: Action) -> str:
    if len(ids) != 1:
        raise ExecutionRefused(f"{action.value} needs exactly one {what}, got {len(ids)}")
    return ids[0]


def plan_writes(proposal: Proposal) -> list[WriteCall]:
    """The ledger calls a proposal would make. Empty list = marks the exception only."""
    t, a = proposal.target, proposal.action
    reason = f"recon-copilot: {proposal.cause.value}"
    match a:
        case Action.LINK_RECORDS:
            return [
                WriteCall(
                    "link_records",
                    {
                        "entry_id": _one(t.ledger_entry_ids, "ledger entry", a),
                        "record_id": _one(t.bank_record_ids, "bank record", a),
                    },
                )
            ]
        case Action.POST_ADJUSTMENT:
            if not t.amount_cents:
                raise ExecutionRefused("post_adjustment needs a non-zero amount_cents")
            return [
                WriteCall(
                    "post_adjustment",
                    {
                        "entry_id": _one(t.ledger_entry_ids, "ledger entry", a),
                        "amount_cents": t.amount_cents,
                        "reason": reason,
                    },
                )
            ]
        case Action.REVERSE_DUPLICATE:
            return [
                WriteCall(
                    "reverse_entry",
                    {"entry_id": _one(t.ledger_entry_ids, "ledger entry", a), "reason": reason},
                )
            ]
        case Action.CREATE_MISSING_ENTRY:
            return [
                WriteCall(
                    "create_entry_from_bank_record",
                    {"record_id": _one(t.bank_record_ids, "bank record", a), "reason": reason},
                )
            ]
        case Action.IGNORE_BANK_DUPLICATE:
            _one(t.bank_record_ids, "bank record", a)
            return []
        case Action.WAIT_NEXT_FILE | Action.ESCALATE_TO_HUMAN:
            raise ExecutionRefused(f"{a.value} has nothing to execute")


async def execute(
    proposal: Proposal,
    approval: Approval,
    ledger: LedgerClient,
    audit: AuditLog,
) -> ExecutionResult:
    if approval.proposal_digest != proposal.digest():
        audit.append("write_refused", approval_id=approval.approval_id, reason="digest mismatch")
        raise ExecutionRefused("approval does not match this proposal")
    if approval.approval_id in audit.used_approval_ids():
        audit.append("write_refused", approval_id=approval.approval_id, reason="approval reused")
        raise ExecutionRefused(f"approval {approval.approval_id} was already used")

    calls = plan_writes(proposal)  # raises before any write if the target is malformed
    result = ExecutionResult(approval_id=approval.approval_id)
    base = {
        "approval_id": approval.approval_id,
        "approver": approval.approver,
        "exception_id": approval.exception_id,
        "proposal_digest": approval.proposal_digest,
    }

    if not calls:
        result.note = f"{proposal.action.value}: exception marked, no ledger write"
        audit.append("write_applied", **base, action=proposal.action.value, calls=[])
        return result

    for call in calls:
        outcome = await ledger.call(
            call.tool, {**call.arguments, "approval_id": approval.approval_id}
        )
        if outcome.is_error:
            audit.append("write_failed", **base, call=call.describe(), error=outcome.text)
            raise ExecutionRefused(f"{call.tool} failed: {outcome.text}")
        result.applied.append({"call": call.describe(), "result": outcome.data})
    audit.append("write_applied", **base, action=proposal.action.value, calls=result.applied)
    return result
