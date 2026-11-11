"""Which ledger-mcp tools the agent may see, and conversion to Anthropic tool definitions."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import Tool as MCPTool

# Read-only tools. Write tools exposed by any ledger-mcp server are dropped here, so the
# model never sees them, and the loop refuses any call to a name not in this set.
READ_TOOL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "get_ledger_entry",
        "search_ledger_entries",
        "get_bank_record",
        "search_bank_records",
        "get_fee_schedule",
        "list_return_files",
    }
)


def to_anthropic_tools(mcp_tools: list[MCPTool]) -> list[dict[str, Any]]:
    """Allowlisted tools only, sorted by name so the prompt prefix is stable (caching)."""
    out = []
    for t in sorted(mcp_tools, key=lambda t: t.name):
        if t.name not in READ_TOOL_ALLOWLIST:
            continue
        out.append(
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": json.loads(json.dumps(t.input_schema, sort_keys=True)),
            }
        )
    return out


_ID_KEYS = ("entry_id", "record_id")


def ids_in(value: Any) -> set[str]:
    """Every ledger entry / bank record ID that appears in a tool result."""
    found: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if k in _ID_KEYS and isinstance(v, str):
                found.add(v)
            else:
                found |= ids_in(v)
    elif isinstance(value, list):
        for v in value:
            found |= ids_in(v)
    return found
