"""In-memory ledger behind the ledger-mcp stub. Pure Python, no MCP, so it is easy to test."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from recon_copilot.domain import BankRecord, Dataset, LedgerEntry

SEARCH_LIMIT_MAX = 25


class NotFound(LookupError):
    pass


def _in_range(d: date, date_from: str | None, date_to: str | None) -> bool:
    if date_from and d < date.fromisoformat(date_from):
        return False
    return not (date_to and d > date.fromisoformat(date_to))


def _amount_ok(amount: int, target: int | None, tolerance: int | None) -> bool:
    if target is None:
        return True
    return abs(amount - target) <= (tolerance or 0)


class Ledger:
    def __init__(self, dataset: Dataset) -> None:
        self._ds = dataset
        self._entries = {e.entry_id: e for e in dataset.ledger_entries}
        self._records = {r.record_id: r for r in dataset.bank_records}
        self.links: list[tuple[str, str]] = []
        self.writes: list[dict[str, Any]] = []

    # ---- reads -------------------------------------------------------------------------

    def get_ledger_entry(self, entry_id: str) -> dict[str, Any]:
        entry = self._entries.get(entry_id)
        if entry is None:
            raise NotFound(f"ledger entry {entry_id!r} not found")
        return entry.model_dump(mode="json")

    def search_ledger_entries(
        self,
        reference: str | None = None,
        amount_cents: int | None = None,
        amount_tolerance_cents: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        counterparty: str | None = None,
        bank_code: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, SEARCH_LIMIT_MAX))
        hits = [
            e
            for e in self._entries.values()
            if (reference is None or reference.lower() in e.reference.lower())
            and _amount_ok(e.amount_cents, amount_cents, amount_tolerance_cents)
            and _in_range(e.posted_on, date_from, date_to)
            and (counterparty is None or counterparty.lower() in e.counterparty.lower())
            and (bank_code is None or e.bank_code == bank_code)
        ]
        hits.sort(key=lambda e: (e.posted_on, e.entry_id))
        return {
            "total_matches": len(hits),
            "returned": min(len(hits), limit),
            "entries": [e.model_dump(mode="json") for e in hits[:limit]],
        }

    def get_bank_record(self, record_id: str) -> dict[str, Any]:
        record = self._records.get(record_id)
        if record is None:
            raise NotFound(f"bank record {record_id!r} not found")
        return record.model_dump(mode="json")

    def search_bank_records(
        self,
        reference: str | None = None,
        amount_cents: int | None = None,
        amount_tolerance_cents: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        payer_name: str | None = None,
        bank_code: str | None = None,
        file_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, SEARCH_LIMIT_MAX))
        hits = [
            r
            for r in self._records.values()
            if (reference is None or reference.lower() in r.reference.lower())
            and _amount_ok(r.amount_cents, amount_cents, amount_tolerance_cents)
            and _in_range(r.settled_on, date_from, date_to)
            and (payer_name is None or payer_name.lower() in r.payer_name.lower())
            and (bank_code is None or r.bank_code == bank_code)
            and (file_id is None or r.file_id == file_id)
        ]
        hits.sort(key=lambda r: (r.settled_on, r.record_id))
        return {
            "total_matches": len(hits),
            "returned": min(len(hits), limit),
            "records": [r.model_dump(mode="json") for r in hits[:limit]],
        }

    def get_fee_schedule(self, bank_code: str, product: str | None = None) -> dict[str, Any]:
        rules = [
            f.model_dump(mode="json")
            for f in self._ds.fee_schedule
            if f.bank_code == bank_code and (product is None or f.product == product)
        ]
        return {"bank_code": bank_code, "rules": rules}

    def list_return_files(
        self, bank_code: str | None = None, date_from: str | None = None, date_to: str | None = None
    ) -> dict[str, Any]:
        files = [
            f.model_dump(mode="json")
            for f in sorted(self._ds.return_files, key=lambda f: (f.delivered_on, f.file_id))
            if (bank_code is None or f.bank_code == bank_code)
            and _in_range(f.delivered_on, date_from, date_to)
        ]
        return {"as_of": self._ds.as_of.isoformat(), "files": files}

    # ---- writes (called only by recon_copilot.executor) ---------------------------------

    def _new_entry_id(self) -> str:
        return f"L-ADJ-{len(self.writes) + 1:04d}"

    @staticmethod
    def _require_approval(approval_id: str) -> None:
        if not approval_id.strip():
            raise PermissionError("write refused: approval_id is required")

    def _record_write(self, kind: str, approval_id: str, **detail: Any) -> dict[str, Any]:
        event = {"kind": kind, "approval_id": approval_id, **detail}
        self.writes.append(event)
        return event

    def post_adjustment(
        self, entry_id: str, amount_cents: int, reason: str, approval_id: str
    ) -> dict[str, Any]:
        self._require_approval(approval_id)
        base = self._entries.get(entry_id)
        if base is None:
            raise NotFound(f"ledger entry {entry_id!r} not found")
        new = base.model_copy(
            update={
                "entry_id": self._new_entry_id(),
                "created_at": datetime.now(UTC),
                "posted_on": self._ds.as_of,
                "amount_cents": amount_cents,
                "memo": f"adjustment of {entry_id}: {reason}",
            }
        )
        self._entries[new.entry_id] = new
        return self._record_write(
            "post_adjustment", approval_id, new_entry_id=new.entry_id, of=entry_id
        )

    def create_entry_from_bank_record(
        self, record_id: str, reason: str, approval_id: str
    ) -> dict[str, Any]:
        self._require_approval(approval_id)
        rec = self._records.get(record_id)
        if rec is None:
            raise NotFound(f"bank record {record_id!r} not found")
        new = LedgerEntry(
            entry_id=self._new_entry_id(),
            created_at=datetime.now(UTC),
            posted_on=rec.settled_on,
            amount_cents=rec.amount_cents,
            reference=rec.reference,
            counterparty=rec.payer_name,
            product=rec.product,
            bank_code=rec.bank_code,
            memo=f"created from {record_id}: {reason}",
        )
        self._entries[new.entry_id] = new
        return self._record_write(
            "create_entry", approval_id, new_entry_id=new.entry_id, from_record=record_id
        )

    def link_records(self, entry_id: str, record_id: str, approval_id: str) -> dict[str, Any]:
        self._require_approval(approval_id)
        if entry_id not in self._entries:
            raise NotFound(f"ledger entry {entry_id!r} not found")
        if record_id not in self._records:
            raise NotFound(f"bank record {record_id!r} not found")
        self.links.append((entry_id, record_id))
        return self._record_write("link", approval_id, entry_id=entry_id, record_id=record_id)

    def reverse_entry(self, entry_id: str, reason: str, approval_id: str) -> dict[str, Any]:
        self._require_approval(approval_id)
        entry = self._entries.get(entry_id)
        if entry is None:
            raise NotFound(f"ledger entry {entry_id!r} not found")
        if entry.status == "reversed":
            raise ValueError(f"ledger entry {entry_id!r} is already reversed")
        self._entries[entry_id] = entry.model_copy(update={"status": "reversed"})
        return self._record_write("reverse", approval_id, entry_id=entry_id, reason=reason)

    # ---- helpers for tests -------------------------------------------------------------

    def entry(self, entry_id: str) -> LedgerEntry:
        return self._entries[entry_id]

    def record(self, record_id: str) -> BankRecord:
        return self._records[record_id]
