"""Connects one chat session to every MCP server, each through its own step-up transport.

Each session gets its own connections so one guest's tokens can never be attached to
another guest's requests. The servers are stateless, so this is cheap.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client

from staywise.config import ServerConfig

logger = logging.getLogger(__name__)

TransportFactory = Callable[[str], httpx.AsyncBaseTransport]


class McpConnectionError(RuntimeError):
    """A configured MCP server could not be reached or initialized."""


@dataclass(frozen=True)
class ToolResult:
    """A tool call's outcome flattened to text for the model."""

    text: str
    ok: bool


class McpHub:
    """Routes tool calls to whichever server owns the tool."""

    def __init__(self) -> None:
        self._owners: dict[str, tuple[str, ClientSession]] = {}
        self._tools: list[types.Tool] = []

    async def add_server(self, name: str, session: ClientSession) -> list[str]:
        """Register an initialized session's tools.

        Raises:
            ValueError: if a tool name is already taken by another server.
        """
        tools = (await session.list_tools()).tools
        for tool in tools:
            if tool.name in self._owners:
                raise ValueError(f"tool {tool.name!r} from {name!r} clashes with an already-registered tool")
        for tool in tools:
            self._owners[tool.name] = (name, session)
        self._tools.extend(tools)
        return [tool.name for tool in tools]

    @property
    def tool_names(self) -> list[str]:
        """Every registered tool, in registration order."""
        return [tool.name for tool in self._tools]

    def server_of(self, tool: str) -> str | None:
        """Name of the server that provides `tool`."""
        owner = self._owners.get(tool)
        return owner[0] if owner else None

    def ollama_tools(self) -> list[dict[str, Any]]:
        """Tool schemas in the function-calling format the model expects."""
        return [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description or "", "parameters": t.inputSchema},
            }
            for t in self._tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Run a tool. Unknown tools and failures come back as error text so the model can react."""
        owner = self._owners.get(name)
        if owner is None:
            return ToolResult(f"Error: unknown tool {name!r}. Available tools: {', '.join(self.tool_names)}", False)
        result = await owner[1].call_tool(name, arguments)
        return format_tool_result(result)


def format_tool_result(result: types.CallToolResult) -> ToolResult:
    """Join the text blocks of a result; a failed call always reads as an error."""
    text = "\n".join(
        block.text if isinstance(block, types.TextContent) else f"[{block.type} content omitted]"
        for block in result.content
    )
    if result.isError and not text.startswith("Error"):
        text = f"Error: {text}"
    return ToolResult(text, not result.isError)


@asynccontextmanager
async def connect(configs: Sequence[ServerConfig], transport_for: TransportFactory) -> AsyncIterator[McpHub]:
    """Open a session to every configured (HTTP) server and yield a hub over all of them.

    Raises:
        McpConnectionError: if a server cannot be reached or initialized.
    """
    async with AsyncExitStack() as stack:
        hub = McpHub()
        for config in configs:
            if not config.url:
                raise McpConnectionError(f"server {config.name!r}: only HTTP servers are supported (tokens need HTTP)")
            try:
                session = await _open_session(stack, config, transport_for(config.name))
                await session.initialize()
                tools = await hub.add_server(config.name, session)
            except (httpx.HTTPError, OSError) as exc:
                raise McpConnectionError(f"cannot connect to MCP server {config.name!r}: {exc}") from exc
            logger.info("connected to %s: %s", config.name, ", ".join(tools))
        yield hub


async def _open_session(
    stack: AsyncExitStack, config: ServerConfig, transport: httpx.AsyncBaseTransport
) -> ClientSession:
    def client_factory(
        headers: dict[str, str] | None = None, timeout: httpx.Timeout | None = None, auth: httpx.Auth | None = None
    ) -> httpx.AsyncClient:
        # No read timeout: a call may wait on the guest's MFA, which has its own timeout.
        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(30, read=None),
            auth=auth,
            transport=transport,
            follow_redirects=True,
        )

    read, write, _ = await stack.enter_async_context(
        streamablehttp_client(config.url or "", headers=dict(config.headers), httpx_client_factory=client_factory)
    )
    return await stack.enter_async_context(ClientSession(read, write))
