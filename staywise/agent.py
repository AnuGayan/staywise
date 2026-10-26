"""The agent loop: ask the model, run the tools it requests over MCP, repeat until it answers."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from ollama import ChatResponse, Message

from staywise.hub import ToolResult

DEFAULT_MAX_STEPS = 10

SYSTEM_PROMPT = """You are StayWise, a hotel concierge for Sri Lankan hotels. Today is {today}. \
You are helping {guest}.

Use the tools for every fact about hotels, rooms, prices, reservations, payments and loyalty points; \
never invent ids, prices or results. Room ids look like 'lighthouse-deluxe'. Dates are YYYY-MM-DD; \
assume the next upcoming occurrence of any month the guest names.

Booking flow: search_hotels or check_availability, then create_reservation, then charge_payment for \
the amount due (deposit_due for refundable rates, the full total for non-refundable ones). For an \
upgrade use change_room, then charge the new balance_due. Only book, charge, change or cancel when \
the guest asked for it in this conversation. Use notify_front_desk or request_housekeeping for \
messages to hotel staff, including the reservation id and the guest's name.

Security: some actions need extra permissions. The platform obtains them for you, and asks the \
guest to verify with MFA when needed; you never see tokens, passwords or codes and must never ask \
the guest for them. If a tool says it is not authorized or the guest declined verification, tell \
the guest plainly what did not happen and offer an alternative; do not retry unless they ask.

Keep answers short and friendly. Show prices in USD."""


class ChatClient(Protocol):
    """The slice of `ollama.AsyncClient` the agent uses (also met by `OpenAIChatClient`)."""

    async def chat(
        self,
        model: str = "",
        messages: Sequence[Mapping[str, Any] | Message] | None = None,
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> ChatResponse:
        """Send the conversation and return the model's next message."""
        ...


class ToolProvider(Protocol):
    """Anything that can describe and run tools (see `McpHub`)."""

    def ollama_tools(self) -> list[dict[str, Any]]:
        """Tool schemas in function-calling format."""
        ...

    def server_of(self, tool: str) -> str | None:
        """Which server provides a tool."""
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Run a tool."""
        ...


@dataclass(frozen=True)
class ToolCallHooks:
    """Called around every tool call, e.g. to stream progress to the UI."""

    on_start: Callable[[str, str, str, dict[str, Any]], None]  # call_id, tool, server, arguments
    on_end: Callable[[str, ToolResult], None]  # call_id, result


class AgentStepLimitError(RuntimeError):
    """The model kept calling tools without producing an answer."""


class Agent:
    """A multi-turn chat agent; keeps its history between `ask` calls."""

    def __init__(
        self,
        llm: ChatClient,
        tools: ToolProvider,
        model: str,
        *,
        guest_name: str,
        max_steps: int = DEFAULT_MAX_STEPS,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._model = model
        self._guest = guest_name
        self._max_steps = max_steps
        self._today = today
        self._messages: list[Mapping[str, Any] | Message] = []
        self.reset()

    def reset(self) -> None:
        """Forget the conversation."""
        prompt = SYSTEM_PROMPT.format(today=self._today().isoformat(), guest=self._guest)
        self._messages = [{"role": "system", "content": prompt}]

    async def ask(self, prompt: str, hooks: ToolCallHooks) -> str:
        """Answer `prompt`, calling tools as needed.

        Raises:
            AgentStepLimitError: if there is still no answer after `max_steps` rounds.
        """
        self._messages.append({"role": "user", "content": prompt})
        schemas = self._tools.ollama_tools()
        for _ in range(self._max_steps):
            response = await self._llm.chat(self._model, self._messages, tools=schemas)
            message = response.message
            self._messages.append(message)
            if not message.tool_calls:
                return message.content or ""
            for call in message.tool_calls:
                await self._run_tool(call.function.name, dict(call.function.arguments), hooks)
        raise AgentStepLimitError(f"no answer after {self._max_steps} tool-calling rounds")

    async def _run_tool(self, name: str, arguments: dict[str, Any], hooks: ToolCallHooks) -> None:
        call_id = uuid.uuid4().hex[:12]
        hooks.on_start(call_id, name, self._tools.server_of(name) or "unknown", arguments)
        result = await self._tools.call_tool(name, arguments)
        hooks.on_end(call_id, result)
        self._messages.append({"role": "tool", "content": result.text, "tool_name": name})
