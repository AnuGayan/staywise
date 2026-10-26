"""Connects to several MCP servers and exposes their tools as one flat catalogue."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Generator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from concierge.config import ServerConfig

logger = logging.getLogger(__name__)


class BearerAuth(httpx.Auth):
    """Adds `Authorization: Bearer <token>` to a server's requests while `token` is set.

    The MCP session sends requests from a task started at connect time, so a per-call token
    can't travel through context variables; the hub sets it here around each call instead.
    """

    def __init__(self) -> None:
        self.token: str | None = None

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """Attach the current token, if any, overriding any static Authorization header."""
        if self.token:
            request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


@dataclass
class _Route:
    """How to reach the server that owns a tool."""

    session: ClientSession
    auth: BearerAuth | None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class McpHub:
    """Routes tool calls to whichever connected server owns the tool."""

    def __init__(self) -> None:
        self._owners: dict[str, _Route] = {}
        self._tools: list[types.Tool] = []
        self.protocol_versions: dict[str, str] = {}

    async def add_server(self, name: str, session: ClientSession, auth: BearerAuth | None = None) -> list[str]:
        """Register an initialized session's tools; return their names. `auth` enables per-call bearer tokens.

        Raises:
            ValueError: if a tool name is already provided by another server.
        """
        tools = await _list_all_tools(session)
        for tool in tools:
            if tool.name in self._owners:
                raise ValueError(f"tool {tool.name!r} from server {name!r} clashes with an already-registered tool")
        route = _Route(session, auth)
        for tool in tools:
            self._owners[tool.name] = route
        self._tools.extend(tools)
        return [tool.name for tool in tools]

    @property
    def tool_names(self) -> list[str]:
        """Names of every registered tool, in registration order."""
        return [tool.name for tool in self._tools]

    def ollama_tools(self) -> list[dict[str, Any]]:
        """Describe every tool in the function-calling format Ollama's /api/chat expects."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema,
                },
            }
            for tool in self._tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any], bearer: str | None = None) -> str:
        """Run a tool and flatten its result to text the model can read.

        `bearer` is sent as `Authorization: Bearer ...` on this call only. Calls to one HTTP server are
        serialized so one guest's token never rides on another guest's request.

        Unknown tools and tool failures come back as an "Error: ..." string rather than
        an exception, so the model can see what went wrong and try again.
        """
        route = self._owners.get(name)
        if route is None:
            return f"Error: unknown tool {name!r}. Available tools: {', '.join(self.tool_names)}"
        if route.auth is None:
            return format_tool_result(await route.session.call_tool(name, arguments))
        async with route.lock:
            route.auth.token = bearer
            try:
                result = await route.session.call_tool(name, arguments)
            finally:
                route.auth.token = None
        return format_tool_result(result)


def format_tool_result(result: types.CallToolResult) -> str:
    """Join the text blocks of a tool result, making sure a failed call reads as an error."""
    parts = [
        block.text if isinstance(block, types.TextContent) else f"[{block.type} content omitted]"
        for block in result.content
    ]
    text = "\n".join(parts)
    if result.isError and not text.startswith("Error"):
        return f"Error: {text}"
    return text


async def _list_all_tools(session: ClientSession) -> list[types.Tool]:
    tools: list[types.Tool] = []
    cursor: str | None = None
    while True:
        page = await session.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.nextCursor
        if cursor is None:
            return tools


class McpConnectionError(RuntimeError):
    """A configured MCP server could not be reached or rejected the handshake."""


@asynccontextmanager
async def connect(configs: Sequence[ServerConfig]) -> AsyncIterator[McpHub]:
    """Open a session to every configured server and yield a hub over all of them.

    Raises:
        McpConnectionError: if a server cannot be reached, spawned, or initialized.
    """
    connecting: str | None = None
    try:
        async with AsyncExitStack() as stack:
            hub = McpHub()
            for config in configs:
                connecting = config.name
                session, auth = await _open_session(stack, config)
                init = await session.initialize()
                hub.protocol_versions[config.name] = str(init.protocolVersion)
                tool_names = await hub.add_server(config.name, session, auth)
                logger.info("connected to %s (MCP %s): %s", config.name, init.protocolVersion, ", ".join(tool_names))
            connecting = None
            yield hub
    except (ExceptionGroup, httpx.HTTPError, OSError) as exc:
        cause = _first_leaf(exc)
        if connecting is None or not isinstance(cause, httpx.HTTPError | OSError):
            raise
        reason = str(cause).splitlines()[0] if str(cause) else type(cause).__name__
        raise McpConnectionError(f"cannot connect to MCP server {connecting!r}: {reason}. {_hint(cause)}") from exc


def _hint(cause: BaseException) -> str:
    status = cause.response.status_code if isinstance(cause, httpx.HTTPStatusError) else None
    if status in (401, 403):
        return "The server or gateway rejected the credentials; check the headers in your MCP config."
    if status == 404:
        return "Nothing is served at that URL; check the path (servers listen on /mcp)."
    return "Is the server running? See README.md, 'Run the MCP servers'."


def _first_leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


async def _open_session(stack: AsyncExitStack, config: ServerConfig) -> tuple[ClientSession, BearerAuth | None]:
    auth: BearerAuth | None = None
    if config.url:
        auth = BearerAuth()
        read, write, _ = await stack.enter_async_context(
            streamablehttp_client(config.url, headers=dict(config.headers), auth=auth)
        )
    else:
        # "python" in the config means "the interpreter running the agent", so an
        # un-activated virtualenv still launches stdio servers with its own packages.
        command = sys.executable if config.command in ("python", "python3") else config.command
        params = StdioServerParameters(command=command or "", args=list(config.args), cwd=config.cwd)
        read, write = await stack.enter_async_context(stdio_client(params))
    return await stack.enter_async_context(ClientSession(read, write)), auth
