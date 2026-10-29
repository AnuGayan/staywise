"""The spans the agent creates for each action: turns, model calls, tool calls, tokens, startup, rejections."""

import json
from contextlib import AsyncExitStack
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from ollama import ChatResponse, Message, ResponseError
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from concierge.agent import Agent
from concierge.config import ServerConfig
from concierge.hub import BearerAuth, McpConnectionError, connect
from concierge.openai_compat import OpenAIChatClient
from concierge.telemetry import TRACE_CONTENT_ENV
from concierge.tokens import GuestTools
from servers import hotel_info_server
from tests.test_http_api import auth, make_client
from tests.test_tokens import FakeThunderTokens, RecordingHub, broker_for


def by_name(spans: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {span.name: span for span in spans.get_finished_spans()}


def attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


class OneToolLlm:
    """Asks for one tool, then answers with the tool's result."""

    async def chat(self, model: str = "", messages: Any = None, *, tools: Any = None) -> ChatResponse:
        last = list(messages or [])[-1]
        if isinstance(last, dict) and last.get("role") == "tool":
            return ChatResponse(message=Message(role="assistant", content=f"done: {last['content']}"))
        call = Message.ToolCall(function=Message.ToolCall.Function(name="get_booking", arguments={"id": "HRM-1"}))
        return ChatResponse(message=Message(role="assistant", tool_calls=[call]))


class FixedTools:
    def __init__(self, result: str) -> None:
        self.result = result

    def ollama_tools(self) -> list[dict[str, Any]]:
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        return self.result


async def test_turn_and_tool_spans_carry_counts_and_content(spans: InMemorySpanExporter) -> None:
    agent = Agent(OneToolLlm(), FixedTools("booking HRM-1"), "m", system_prompt="s")
    await agent.ask("show HRM-1", trace_attributes={"gen_ai.conversation.id": "c1"})

    finished = by_name(spans)
    turn, tool = attrs(finished["melody.agent"]), finished["execute_tool get_booking"]
    assert turn["gen_ai.request.model"] == "m"
    assert turn["gen_ai.conversation.id"] == "c1"
    assert (turn["melody.llm_calls"], turn["melody.tool_calls"]) == (2, 1)
    assert (turn["traceloop.entity.input"], turn["traceloop.entity.output"]) == ("show HRM-1", "done: booking HRM-1")
    assert attrs(tool)["gen_ai.tool.call.arguments"] == '{"id": "HRM-1"}'
    assert attrs(tool)["gen_ai.tool.call.result"] == "booking HRM-1"
    assert tool.status.status_code == StatusCode.UNSET


async def test_tool_errors_mark_the_tool_span_failed(spans: InMemorySpanExporter) -> None:
    agent = Agent(OneToolLlm(), FixedTools("Error: not authorized to use get_booking"), "m", system_prompt="s")
    await agent.ask("show HRM-1")

    tool = by_name(spans)["execute_tool get_booking"]
    assert tool.status.status_code == StatusCode.ERROR
    assert attrs(tool)["error.type"] == "tool_error"


async def test_content_tracing_off_keeps_payloads_out(
    spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TRACE_CONTENT_ENV, "false")
    await Agent(OneToolLlm(), FixedTools("secret result"), "m", system_prompt="s").ask("private question")

    for span in spans.get_finished_spans():
        values = json.dumps(attrs(span), default=str)
        assert "private question" not in values and "secret result" not in values


def openai_client(response: httpx.Response) -> OpenAIChatClient:
    return OpenAIChatClient("http://gw", reasoning_effort="none", transport=httpx.MockTransport(lambda _: response))


async def test_model_calls_over_the_gateway_get_llm_spans(spans: InMemorySpanExporter) -> None:
    body = {
        "id": "chatcmpl-1",
        "model": "gpt-x-2026",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "tool_calls": [{"id": "c0", "function": {"name": "get_hotel_info", "arguments": "{}"}}],
                },
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 8},
    }
    client = openai_client(httpx.Response(200, json=body))
    tools = [{"type": "function", "function": {"name": "get_hotel_info", "description": "Hotel facts"}}]
    reply = await client.chat("gpt-x", [{"role": "user", "content": "check-in time?"}], tools=tools)

    assert (reply.prompt_eval_count, reply.eval_count) == (120, 8)
    span = by_name(spans)["chat gpt-x"]
    values = attrs(span)
    assert values["llm.request.type"] == "chat"
    assert values["gen_ai.request.model"] == "gpt-x"
    assert values["gen_ai.response.model"] == "gpt-x-2026"
    assert values["gen_ai.request.reasoning_effort"] == "none"
    assert values["gen_ai.response.finish_reasons"] == ("tool_calls",)
    assert (values["gen_ai.usage.input_tokens"], values["gen_ai.usage.output_tokens"]) == (120, 8)
    assert values["llm.usage.total_tokens"] == 128
    assert values["gen_ai.prompt.0.content"] == "check-in time?"
    assert values["llm.request.functions.0.name"] == "get_hotel_info"
    assert values["gen_ai.completion.0.tool_calls.0.name"] == "get_hotel_info"
    assert values["http.response.status_code"] == 200


async def test_rejected_model_calls_mark_the_llm_span_failed(spans: InMemorySpanExporter) -> None:
    client = openai_client(httpx.Response(400, json={"error": {"message": "bad param"}}))
    with pytest.raises(ResponseError):
        await client.chat("gpt-x", [{"role": "user", "content": "hi"}])

    span = by_name(spans)["chat gpt-x"]
    assert span.status.status_code == StatusCode.ERROR
    assert attrs(span)["http.response.status_code"] == 400


async def test_token_spans_show_exchanges_cache_hits_and_the_credential(spans: InMemorySpanExporter) -> None:
    tools = GuestTools(RecordingHub(), broker_for(FakeThunderTokens()))
    tools.user_token = "user-at"
    await tools.call_tool("get_unity_balance", {})
    await tools.call_tool("get_unity_balance", {})

    exchanges = [s for s in spans.get_finished_spans() if s.name == "token_exchange"]
    assert [attrs(s)["oauth.token.cached"] for s in exchanges] == [False, True]
    assert attrs(exchanges[0])["oauth.scope"] == "unity:read"
    assert attrs(exchanges[0])["http.response.status_code"] == 200
    agent_token = by_name(spans)["agent_token"]
    assert agent_token.parent is not None and agent_token.parent.span_id == exchanges[0].context.span_id
    assert all("user-at" not in json.dumps(attrs(s)) for s in spans.get_finished_spans())


async def test_refused_exchange_marks_the_token_span_failed(spans: InMemorySpanExporter) -> None:
    tools = GuestTools(RecordingHub(), broker_for(FakeThunderTokens(user_scopes="hotel:read")))
    tools.user_token = "user-at"
    assert (await tools.call_tool("create_booking", {})).startswith("Error: not authorized")

    exchange = by_name(spans)["token_exchange"]
    assert exchange.status.status_code == StatusCode.ERROR


async def test_startup_has_a_span_per_mcp_server(spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch) -> None:
    from concierge import hub as hub_module

    async def in_memory_session(stack: AsyncExitStack, config: ServerConfig) -> tuple[ClientSession, BearerAuth]:
        server = hotel_info_server.create_server()._mcp_server
        return await stack.enter_async_context(create_connected_server_and_client_session(server)), BearerAuth()

    monkeypatch.setattr(hub_module, "_open_session", in_memory_session)
    async with connect([ServerConfig(name="hotel-info", url="http://gw/mcp")]):
        pass

    finished = by_name(spans)
    server = finished["mcp.connect hotel-info"]
    assert attrs(server)["mcp.tools"] == ("get_hotel_info", "list_room_types")
    assert server.parent is not None and server.parent.span_id == finished["melody.startup"].context.span_id


async def test_failed_startup_shows_which_server(spans: InMemorySpanExporter) -> None:
    with pytest.raises(McpConnectionError):
        async with connect([ServerConfig(name="reservations", url="http://127.0.0.1:9/mcp")]):
            pytest.fail("should not connect")

    assert by_name(spans)["mcp.connect reservations"].status.status_code == StatusCode.ERROR


def test_rejected_requests_leave_a_span(spans: InMemorySpanExporter) -> None:
    assert make_client().post("/chat", json={"message": "hi"}).status_code == 401

    rejected = by_name(spans)["melody.request_rejected"]
    assert attrs(rejected)["http.response.status_code"] == 401
    assert attrs(rejected)["error.type"] == "no_user_token"


def test_turns_name_the_guest_and_conversation(spans: InMemorySpanExporter) -> None:
    response = make_client().post("/chat", json={"message": "hi", "conversation_id": "c9"}, headers=auth("priya"))
    assert response.status_code == 200

    turn = attrs(by_name(spans)["melody.agent"])
    assert (turn["gen_ai.conversation.id"], turn["enduser.id"]) == ("c9", "HR-100377")
