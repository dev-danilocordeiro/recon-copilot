"""ledger-mcp stub: an MCP server over stdio that serves a synthetic dataset.

The tool names and arguments are the ledger-mcp contract (docs/plan.md, section 5).
Read tools are safe for the agent. Write tools exist so the executor has something to
call; the agent never receives them (see recon_copilot.agent.tools.READ_TOOL_ALLOWLIST).
"""

from __future__ import annotations

import argparse
import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ledger_mcp_stub.ledger import Ledger, NotFound
from recon_copilot.domain import Dataset


def _expected_errors[**P](fn: Callable[P, dict[str, Any]]) -> Callable[P, dict[str, Any]]:
    """Turn domain errors into ToolError so the caller sees the message, not a generic
    'unexpected exception'."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except (NotFound, ValueError, PermissionError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def build_server(ledger: Ledger) -> MCPServer:
    server = MCPServer(
        "ledger-mcp-stub",
        instructions="Synthetic ledger for recon-copilot. All data is fictitious.",
    )

    @server.tool()
    @_expected_errors
    def get_ledger_entry(entry_id: str) -> dict[str, Any]:
        """Fetch one ledger entry by ID."""
        return ledger.get_ledger_entry(entry_id)

    @server.tool()
    @_expected_errors
    def search_ledger_entries(
        reference: str | None = None,
        amount_cents: int | None = None,
        amount_tolerance_cents: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        counterparty: str | None = None,
        bank_code: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search ledger entries. All filters are optional and combined with AND.
        reference and counterparty match case-insensitive substrings. Dates are ISO
        (YYYY-MM-DD), inclusive, applied to posted_on. Amounts are integer cents.
        Results are sorted by posted_on then entry_id and capped at `limit` (max 25);
        total_matches tells you if there were more."""
        return ledger.search_ledger_entries(
            reference,
            amount_cents,
            amount_tolerance_cents,
            date_from,
            date_to,
            counterparty,
            bank_code,
            limit,
        )

    @server.tool()
    @_expected_errors
    def get_bank_record(record_id: str) -> dict[str, Any]:
        """Fetch one bank record (a line from a bank return file) by ID."""
        return ledger.get_bank_record(record_id)

    @server.tool()
    @_expected_errors
    def search_bank_records(
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
        """Search bank records from return files. Filters work like search_ledger_entries;
        dates apply to settled_on."""
        return ledger.search_bank_records(
            reference,
            amount_cents,
            amount_tolerance_cents,
            date_from,
            date_to,
            payer_name,
            bank_code,
            file_id,
            limit,
        )

    @server.tool()
    @_expected_errors
    def get_fee_schedule(bank_code: str, product: str | None = None) -> dict[str, Any]:
        """Fee the bank deducts per settled item, in cents, by product (boleto or pix)."""
        return ledger.get_fee_schedule(bank_code, product)

    @server.tool()
    @_expected_errors
    def list_return_files(
        bank_code: str | None = None, date_from: str | None = None, date_to: str | None = None
    ) -> dict[str, Any]:
        """Return files received from the banks, with the delivery date and the settlement
        date each file covers. Also returns as_of, the current business date."""
        return ledger.list_return_files(bank_code, date_from, date_to)

    # ---- write tools: executor only --------------------------------------------------

    @server.tool()
    @_expected_errors
    def post_adjustment(
        entry_id: str, amount_cents: int, reason: str, approval_id: str
    ) -> dict[str, Any]:
        """WRITE. Post an adjusting entry against entry_id. Requires a human approval_id."""
        return ledger.post_adjustment(entry_id, amount_cents, reason, approval_id)

    @server.tool()
    @_expected_errors
    def create_entry_from_bank_record(
        record_id: str, reason: str, approval_id: str
    ) -> dict[str, Any]:
        """WRITE. Create a ledger entry from a bank record. Requires a human approval_id."""
        return ledger.create_entry_from_bank_record(record_id, reason, approval_id)

    @server.tool()
    @_expected_errors
    def link_records(entry_id: str, record_id: str, approval_id: str) -> dict[str, Any]:
        """WRITE. Link a ledger entry to a bank record. Requires a human approval_id."""
        return ledger.link_records(entry_id, record_id, approval_id)

    @server.tool()
    @_expected_errors
    def reverse_entry(entry_id: str, reason: str, approval_id: str) -> dict[str, Any]:
        """WRITE. Reverse a ledger entry. Requires a human approval_id."""
        return ledger.reverse_entry(entry_id, reason, approval_id)

    return server


def load_dataset(path: str | Path) -> Dataset:
    return Dataset.model_validate_json(Path(path).read_text())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ledger-mcp-stub")
    parser.add_argument("--dataset", required=True, help="dataset.json produced by datagen")
    args = parser.parse_args(argv)
    build_server(Ledger(load_dataset(args.dataset))).run(transport="stdio")
