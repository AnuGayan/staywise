import json

import httpx
import pytest
from ollama import Message, ResponseError

from staywise.openai_compat import OpenAIChatClient, from_openai_response, to_openai_messages


def tool_call(name: str, **arguments: object) -> Message.ToolCall:
    return Message.ToolCall(function=Message.ToolCall.Function(name=name, arguments=arguments))


def test_to_openai_messages_pairs_tool_results_with_call_ids() -> None:
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        Message(role="assistant", tool_calls=[tool_call("calculate", expression="1+1"), tool_call("list_notes")]),
        {"role": "tool", "content": "2", "tool_name": "calculate"},
        {"role": "tool", "content": "[]", "tool_name": "list_notes"},
        Message(role="assistant", content="done"),
    ]
    converted = to_openai_messages(history)
    assert converted[0] == {"role": "system", "content": "sys"}
    assert converted[2]["tool_calls"] == [
        {"id": "call_2_0", "type": "function", "function": {"name": "calculate", "arguments": '{"expression": "1+1"}'}},
        {"id": "call_2_1", "type": "function", "function": {"name": "list_notes", "arguments": "{}"}},
    ]
    assert converted[3] == {"role": "tool", "tool_call_id": "call_2_0", "content": "2"}
    assert converted[4] == {"role": "tool", "tool_call_id": "call_2_1", "content": "[]"}
    assert converted[5] == {"role": "assistant", "content": "done"}


def test_to_openai_messages_rejects_orphan_tool_result() -> None:
    with pytest.raises(ValueError, match="no matching tool call"):
        to_openai_messages([{"role": "tool", "content": "x", "tool_name": "calculate"}])


def test_from_openai_response_parses_tool_calls() -> None:
    body = {
        "model": "qwen3:1.7b",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "calculate", "arguments": '{"expression":"6*7"}'},
                        },
                        {"id": "c2", "type": "function", "function": {"name": "list_notes", "arguments": ""}},
                    ],
                },
            }
        ],
    }
    message = from_openai_response(body).message
    assert message.content == ""
    assert [(c.function.name, dict(c.function.arguments)) for c in message.tool_calls or []] == [
        ("calculate", {"expression": "6*7"}),
        ("list_notes", {}),
    ]


def test_from_openai_response_rejects_non_object_arguments() -> None:
    body = {"choices": [{"message": {"tool_calls": [{"function": {"name": "f", "arguments": "[1]"}}]}}]}
    with pytest.raises(ValueError, match="JSON object"):
        from_openai_response(body)


async def test_chat_posts_openai_payload_and_returns_reply() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "hello"}}]})

    client = OpenAIChatClient("http://gw/ollama/", api_key="k", transport=httpx.MockTransport(handler))
    try:
        reply = await client.chat("m", [{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    finally:
        await client.aclose()

    assert reply.message.content == "hello"
    assert str(seen[0].url) == "http://gw/ollama/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer k"
    assert json.loads(seen[0].content) == {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function"}],
    }


@pytest.mark.parametrize(
    ("response", "status"),
    [(httpx.Response(401, text="Invalid Credentials"), 401), (httpx.Response(200, text="not json"), 200)],
)
async def test_chat_maps_bad_responses_to_response_error(response: httpx.Response, status: int) -> None:
    client = OpenAIChatClient("http://gw", transport=httpx.MockTransport(lambda _: response))
    try:
        with pytest.raises(ResponseError) as raised:
            await client.chat("m", [{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    assert raised.value.status_code == status


async def test_chat_maps_transport_failures_to_connection_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = OpenAIChatClient("http://gw", transport=httpx.MockTransport(refuse))
    try:
        with pytest.raises(ConnectionError, match="refused"):
            await client.chat("m", [{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("header", "expected"),
    [("Authorization", "Bearer k"), ("X-API-Key", "k")],
)
async def test_chat_sends_api_key_in_configured_header(header: str, expected: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAIChatClient("http://gw", api_key="k", api_key_header=header, transport=httpx.MockTransport(handler))
    try:
        await client.chat("m", [{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()
    assert seen[0].headers[header] == expected
