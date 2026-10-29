"""The concierge agent loop: ask the model, run any tools it requests over MCP, repeat."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any, Protocol

from ollama import ChatResponse, Message
from opentelemetry.trace import Span

from concierge.telemetry import AGENT_NAME, mark_error, set_content, tracer

DEFAULT_MEMBER_ID = "HR-100245"
_SYSTEM_PROMPT = """You are Melody, the concierge for Hard Rock Hotel Miami. If asked who you are, you are Melody.
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

# One parent span per guest turn, so the model, token and MCP spans nest into a single trace. The
# attributes mark it as an agent span the way Traceloop's @agent decorator does. Without a configured
# tracer provider (no `amp-instrument`) spans are no-ops.
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

    async def ask(self, prompt: str, *, trace_attributes: Mapping[str, str] | None = None) -> str:
        """Answer `prompt`, calling tools as many times as the model needs (up to `max_steps`).

        `trace_attributes` (e.g. the conversation id) are added to the turn's span.

        Raises:
            AgentStepLimitError: if the model is still calling tools after `max_steps` rounds.
        """
        attributes = {**_TURN_SPAN_ATTRIBUTES, "gen_ai.request.model": self._model, **(trace_attributes or {})}
        with tracer.start_as_current_span(f"{AGENT_NAME}.agent", attributes=attributes) as span:
            set_content(span, "traceloop.entity.input", prompt)
            reply = await self._answer(prompt, span)
            set_content(span, "traceloop.entity.output", reply)
            return reply

    async def _answer(self, prompt: str, span: Span) -> str:
        self._messages.append({"role": "user", "content": prompt})
        tool_schemas = self._tools.ollama_tools()
        tool_calls = 0
        for step in range(1, self._max_steps + 1):
            response = await self._llm.chat(self._model, self._messages, tools=tool_schemas)
            span.set_attribute("melody.llm_calls", step)
            message = response.message
            self._messages.append(message)
            if not message.tool_calls:
                return message.content or ""
            for call in message.tool_calls:
                await self._run_tool(call.function.name, dict(call.function.arguments))
                tool_calls += 1
                span.set_attribute("melody.tool_calls", tool_calls)
        raise AgentStepLimitError(f"no final answer after {self._max_steps} tool-calling rounds")

    async def _run_tool(self, name: str, arguments: dict[str, Any]) -> None:
        # The agent-side view of one tool call: its token, the MCP call (Traceloop's `<tool>.tool` span)
        # and the outcome, including refusals that never reach an MCP server.
        attributes = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "traceloop.span.kind": "task",
            "traceloop.entity.name": name,
        }
        with tracer.start_as_current_span(f"execute_tool {name}", attributes=attributes) as span:
            set_content(span, "gen_ai.tool.call.arguments", arguments)
            result = await self._tools.call_tool(name, arguments)
            set_content(span, "gen_ai.tool.call.result", result)
            if result.startswith("Error"):
                mark_error(span, result.splitlines()[0][:200], "tool_error")
        if self._on_tool_call is not None:
            self._on_tool_call(name, arguments, result)
        self._messages.append({"role": "tool", "content": result, "tool_name": name})
