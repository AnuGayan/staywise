from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, ClassVar

import pytest
from ollama import ChatResponse, Message
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from concierge.agent import DEFAULT_MEMBER_ID, Agent, AgentStepLimitError, build_system_prompt


def tool_call(name: str, **arguments: Any) -> Message.ToolCall:
    return Message.ToolCall(function=Message.ToolCall.Function(name=name, arguments=arguments))


class ScriptedLlm:
    """Returns pre-written assistant messages in order and records what it was sent."""

    def __init__(self, *replies: Message) -> None:
        self._replies = list(replies)
        self.requests: list[list[Mapping[str, Any] | Message]] = []

    async def chat(
        self,
        model: str = "",
        messages: Sequence[Mapping[str, Any] | Message] | None = None,
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> ChatResponse:
        assert model == "test-model"
        assert tools == FakeTools.SCHEMAS
        self.requests.append(list(messages or []))
        return ChatResponse(model=model, message=self._replies.pop(0))


class FakeTools:
    SCHEMAS: ClassVar[list[dict[str, Any]]] = [{"type": "function", "function": {"name": "calculate"}}]

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def ollama_tools(self) -> list[dict[str, Any]]:
        return self.SCHEMAS

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        return f"{name} ok"


async def test_answers_directly_when_no_tool_is_needed() -> None:
    llm = ScriptedLlm(Message(role="assistant", content="Hello!"))
    agent = Agent(llm, FakeTools(), "test-model", system_prompt="sys")
    assert await agent.ask("hi") == "Hello!"
    assert llm.requests[0] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


async def test_runs_requested_tools_and_feeds_results_back() -> None:
    llm = ScriptedLlm(
        Message(role="assistant", tool_calls=[tool_call("calculate", expression="1+1"), tool_call("list_bookings")]),
        Message(role="assistant", content="1+1 is 2 and you have bookings."),
    )
    tools = FakeTools()
    seen: list[tuple[str, Mapping[str, Any], str]] = []
    agent = Agent(llm, tools, "test-model", on_tool_call=lambda *call: seen.append(call))

    assert await agent.ask("sum and bookings?") == "1+1 is 2 and you have bookings."
    assert tools.calls == [("calculate", {"expression": "1+1"}), ("list_bookings", {})]
    assert [name for name, _, _ in seen] == ["calculate", "list_bookings"]
    assert llm.requests[1][-2:] == [
        {"role": "tool", "content": "calculate ok", "tool_name": "calculate"},
        {"role": "tool", "content": "list_bookings ok", "tool_name": "list_bookings"},
    ]


async def test_keeps_history_until_reset() -> None:
    llm = ScriptedLlm(*(Message(role="assistant", content=text) for text in ("a", "b", "c")))
    agent = Agent(llm, FakeTools(), "test-model")
    await agent.ask("one")
    await agent.ask("two")
    assert len(llm.requests[1]) == 4  # system, user, assistant, user
    agent.reset()
    await agent.ask("three")
    assert len(llm.requests[2]) == 2


async def test_gives_up_after_max_steps() -> None:
    looping = Message(role="assistant", tool_calls=[tool_call("calculate", expression="1")])
    agent = Agent(ScriptedLlm(looping, looping), FakeTools(), "test-model", max_steps=2)
    with pytest.raises(AgentStepLimitError, match="2 tool-calling rounds"):
        await agent.ask("loop forever")


def test_system_prompt_pins_member_and_date() -> None:
    prompt = build_system_prompt("HR-100377", date(2026, 10, 26))
    assert "You are Melody, the concierge for Hard Rock Hotel Miami." in prompt
    assert 'member_id="HR-100377"' in prompt
    assert "Today is Monday, 2026-10-26." in prompt


async def test_default_prompt_is_for_the_demo_member() -> None:
    llm = ScriptedLlm(Message(role="assistant", content="hi"))
    await Agent(llm, FakeTools(), "test-model").ask("hi")
    system = llm.requests[0][0]
    assert isinstance(system, dict)
    assert f'member_id="{DEFAULT_MEMBER_ID}"' in system["content"]


@pytest.fixture(scope="module")
def spans() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)  # the agent's module tracer is a proxy that resolves lazily
    return exporter


async def test_each_turn_is_one_agent_span_parenting_the_tool_work(spans: InMemorySpanExporter) -> None:
    spans.clear()
    child_parents: list[int | None] = []

    class TracedTools(FakeTools):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
            with trace.get_tracer("test").start_as_current_span("tool") as span:
                parent = span.parent
                child_parents.append(parent.span_id if parent else None)
            return await super().call_tool(name, arguments)

    llm = ScriptedLlm(
        Message(role="assistant", tool_calls=[tool_call("calculate", expression="1")]),
        Message(role="assistant", content="done"),
    )
    await Agent(llm, TracedTools(), "test-model").ask("go")

    turn = next(span for span in spans.get_finished_spans() if span.name == "melody.agent")
    assert turn.attributes is not None
    assert turn.attributes["traceloop.span.kind"] == "agent"
    assert turn.attributes["gen_ai.agent.name"] == "melody"
    assert child_parents == [turn.context.span_id]
