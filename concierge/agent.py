"""The concierge agent loop: ask the model, run any tools it requests over MCP, repeat."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any, Protocol

from ollama import ChatResponse, Message
from opentelemetry import trace

DEFAULT_MEMBER_ID = "HR-100245"
_SYSTEM_PROMPT = """You are the concierge for Hard Rock Hotel Miami.
You are chatting with a signed-in Unity by Hard Rock member.
The guest's Unity member id is {member_id}. Today is {weekday}, {today}.

Rules:
- Use the tools for every fact about the hotel, rooms, availability, bookings and Unity points. Never invent them.
- Always pass member_id="{member_id}". Never ask the guest for it and never use any other member id.
- Dates are YYYY-MM-DD. check_out is the departure day. Work out relative dates such as "next Friday" from today.
- To book: call check_availability first. If the guest gave the room type, dates and guest count and the room is
  free, call create_booking; otherwise ask only for what is missing. Quote the confirmation number when done.
- Only call cancel_booking when the guest explicitly asks to cancel a specific booking.
- If a tool returns an error, fix the input and try again, or explain the problem.
Keep answers short and friendly."""


def build_system_prompt(member_id: str, today: date) -> str:
    """The concierge instructions for one signed-in guest, anchored to today's date."""
    return _SYSTEM_PROMPT.format(member_id=member_id, today=today.isoformat(), weekday=today.strftime("%A"))


DEFAULT_MAX_STEPS = 8
AGENT_NAME = "hardrock_concierge"

# One parent span per guest turn, so the model and MCP spans Traceloop records nest into a single
# trace. The attributes mark it as an agent span the way Traceloop's @agent decorator does. Without a
# configured tracer provider (no `amp-instrument`) this is a no-op.
_tracer = trace.get_tracer(__name__)
_TURN_SPAN_ATTRIBUTES = {
    "traceloop.span.kind": "agent",
    "traceloop.entity.name": AGENT_NAME,
    "gen_ai.operation.name": "invoke_agent",
    "gen_ai.agent.name": AGENT_NAME,
}

ToolCallObserver = Callable[[str, Mapping[str, Any], str], None]


class ChatClient(Protocol):
    """The slice of `ollama.AsyncClient` the agent uses."""

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
    """Anything that can describe tools to the model and execute them (see `McpHub`)."""

    def ollama_tools(self) -> list[dict[str, Any]]:
        """Tool schemas in Ollama's function-calling format."""
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Run a tool and return its result as text."""
        ...


class AgentStepLimitError(RuntimeError):
    """The model kept requesting tools without producing a final answer."""


class Agent:
    """A multi-turn chat agent that keeps its conversation history between calls to `ask`."""

    def __init__(
        self,
        llm: ChatClient,
        tools: ToolProvider,
        model: str,
        *,
        system_prompt: str | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        on_tool_call: ToolCallObserver | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._model = model
        self._system_prompt = system_prompt or build_system_prompt(DEFAULT_MEMBER_ID, date.today())
        self._max_steps = max_steps
        self._on_tool_call = on_tool_call
        self._messages: list[Mapping[str, Any] | Message] = []
        self.reset()

    def reset(self) -> None:
        """Forget the conversation, keeping only the system prompt."""
        self._messages = [{"role": "system", "content": self._system_prompt}]

    async def ask(self, prompt: str) -> str:
        """Answer `prompt`, calling tools as many times as the model needs (up to `max_steps`).

        Raises:
            AgentStepLimitError: if the model is still calling tools after `max_steps` rounds.
        """
        with _tracer.start_as_current_span(f"{AGENT_NAME}.agent", attributes=_TURN_SPAN_ATTRIBUTES):
            return await self._answer(prompt)

    async def _answer(self, prompt: str) -> str:
        self._messages.append({"role": "user", "content": prompt})
        tool_schemas = self._tools.ollama_tools()
        for _ in range(self._max_steps):
            response = await self._llm.chat(self._model, self._messages, tools=tool_schemas)
            message = response.message
            self._messages.append(message)
            if not message.tool_calls:
                return message.content or ""
            for call in message.tool_calls:
                await self._run_tool(call.function.name, dict(call.function.arguments))
        raise AgentStepLimitError(f"no final answer after {self._max_steps} tool-calling rounds")

    async def _run_tool(self, name: str, arguments: dict[str, Any]) -> None:
        result = await self._tools.call_tool(name, arguments)
        if self._on_tool_call is not None:
            self._on_tool_call(name, arguments, result)
        self._messages.append({"role": "tool", "content": result, "tool_name": name})
