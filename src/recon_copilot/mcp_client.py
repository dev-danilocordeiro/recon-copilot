"""Connection to a ledger-mcp server (real or stub)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent
from mcp.types import Tool as MCPTool


@dataclass(frozen=True)
class ToolOutcome:
    is_error: bool
    text: str  # what goes back to the model
    data: Any  # parsed JSON when the result was JSON, else None


class LedgerClient:
    def __init__(self, client: Client) -> None:
        self._client = client

    async def list_tools(self) -> list[MCPTool]:
        tools: list[MCPTool] = []
        cursor: str | None = None
        while True:
            page = await self._client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            if page.next_cursor is None:
                return tools
            cursor = page.next_cursor

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        result = await self._client.call_tool(name, arguments)
        text = "\n".join(c.text for c in result.content if isinstance(c, TextContent))
        data: Any = result.structured_content
        if data is None:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        return ToolOutcome(is_error=bool(result.is_error), text=text, data=data)


@asynccontextmanager
async def connect_stdio(command: list[str]) -> AsyncIterator[LedgerClient]:
    params = StdioServerParameters(command=command[0], args=command[1:])
    async with Client(params) as client:
        yield LedgerClient(client)


@asynccontextmanager
async def connect_in_process(server: MCPServer) -> AsyncIterator[LedgerClient]:
    async with Client(server) as client:
        yield LedgerClient(client)
