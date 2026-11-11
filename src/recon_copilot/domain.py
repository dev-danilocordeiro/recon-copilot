"""Domain types shared by the agent, the stub ledger, the data generator and the evals.

Money is always integer cents (BRL). Dates are ISO dates.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Cause(StrEnum):
    TIMING_DIFFERENCE = "timing_difference"
    BANK_FEE_DEDUCTED = "bank_fee_deducted"
    INTEREST_OR_PENALTY = "interest_or_penalty"
    PARTIAL_PAYMENT = "partial_payment"
    DUPLICATE_LEDGER_ENTRY = "duplicate_ledger_entry"
    DUPLICATE_BANK_RECORD = "duplicate_bank_record"
    WRONG_REFERENCE = "wrong_reference"
    MISSING_LEDGER_ENTRY = "missing_ledger_entry"
    UNKNOWN = "unknown"


class Action(StrEnum):
    WAIT_NEXT_FILE = "wait_next_file"
    LINK_RECORDS = "link_records"
    POST_ADJUSTMENT = "post_adjustment"
    REVERSE_DUPLICATE = "reverse_duplicate"
    CREATE_MISSING_ENTRY = "create_missing_entry"
    IGNORE_BANK_DUPLICATE = "ignore_bank_duplicate"
    ESCALATE_TO_HUMAN = "escalate_to_human"


# Actions that change the ledger. These are only ever proposed by the agent and only
# executed by recon_copilot.executor after a human approval.
WRITE_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.LINK_RECORDS,
        Action.POST_ADJUSTMENT,
        Action.REVERSE_DUPLICATE,
        Action.CREATE_MISSING_ENTRY,
    }
)

# Actions that carry an amount which must match exactly.
AMOUNT_ACTIONS: frozenset[Action] = frozenset({Action.POST_ADJUSTMENT, Action.CREATE_MISSING_ENTRY})


class ExceptionType(StrEnum):
    UNMATCHED_BANK = "unmatched_bank"  # bank record with no ledger entry
    UNMATCHED_LEDGER = "unmatched_ledger"  # ledger entry with no bank record
    AMOUNT_MISMATCH = "amount_mismatch"
    DUPLICATE = "duplicate"


Product = Literal["boleto", "pix"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LedgerEntry(_Frozen):
    entry_id: str
    created_at: datetime
    posted_on: date
    due_on: date | None = None
    amount_cents: int
    reference: str
    counterparty: str
    product: Product
    bank_code: str
    status: Literal["posted", "reversed"] = "posted"
    memo: str = ""


class BankRecord(_Frozen):
    record_id: str
    file_id: str
    bank_code: str
    settled_on: date
    amount_cents: int
    reference: str
    payer_name: str
    product: Product
    memo: str = ""


class ReturnFile(_Frozen):
    file_id: str
    bank_code: str
    delivered_on: date
    covers_settlement_on: date


class FeeRule(_Frozen):
    bank_code: str
    product: Product
    fee_cents: int


class ReconException(_Frozen):
    exception_id: str
    type: ExceptionType
    detected_on: date
    ledger_entry_ids: list[str] = Field(default_factory=list)
    bank_record_ids: list[str] = Field(default_factory=list)
    note: str = ""


class Dataset(BaseModel):
    """Everything the ledger-mcp stub serves."""

    model_config = ConfigDict(extra="forbid")

    as_of: date
    ledger_entries: list[LedgerEntry]
    bank_records: list[BankRecord]
    return_files: list[ReturnFile]
    fee_schedule: list[FeeRule]


class ProposalTarget(_Frozen):
    ledger_entry_ids: list[str]
    bank_record_ids: list[str]
    amount_cents: int | None


class Evidence(_Frozen):
    source_id: str
    fact: str


class Proposal(_Frozen):
    cause: Cause
    action: Action
    target: ProposalTarget
    evidence: list[Evidence]
    confidence: Literal["high", "medium", "low"]
    rationale: str

    def digest(self) -> str:
        """Stable hash of the proposal, used to bind an approval to exactly this proposal."""
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def proposal_json_schema() -> dict[str, object]:
    """JSON schema for structured outputs. Written by hand so it stays within what
    structured outputs accept (closed objects, every property required)."""
    id_list: dict[str, object] = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["cause", "action", "target", "evidence", "confidence", "rationale"],
        "properties": {
            "cause": {"type": "string", "enum": [c.value for c in Cause]},
            "action": {"type": "string", "enum": [a.value for a in Action]},
            "target": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ledger_entry_ids", "bank_record_ids", "amount_cents"],
                "properties": {
                    "ledger_entry_ids": id_list,
                    "bank_record_ids": id_list,
                    "amount_cents": {"type": ["integer", "null"]},
                },
            },
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source_id", "fact"],
                    "properties": {
                        "source_id": {"type": "string"},
                        "fact": {"type": "string"},
                    },
                },
            },
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "rationale": {"type": "string"},
        },
    }
