from collections.abc import Mapping, Sequence
from typing import Any

import jwt
import pytest
from ollama import ChatResponse, Message, ResponseError
from starlette.testclient import TestClient

from concierge.agent import Agent, AgentStepLimitError, ToolCallObserver
from concierge.http_api import Runtime, create_app
from concierge.identity import Guest

MEMBERS = {"alex": "HR-100245", "priya": "HR-100377"}


def token_for(username: str, sub: str | None = None) -> str:
    return jwt.encode({"sub": sub or f"sub-{username}", "username": username}, "k" * 32, "HS256")


def auth(username: str = "alex", sub: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(username, sub)}"}


class EchoLlm:
    """Calls `get_unity_balance` when asked about points; otherwise echoes the system prompt (the member id)."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    async def chat(
        self,
        model: str = "",
        messages: Sequence[Mapping[str, Any] | Message] | None = None,
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> ChatResponse:
        if self._error:
            raise self._error
        history = list(messages or [])
        last = history[-1]
        if isinstance(last, dict) and last.get("role") == "tool":
            return ChatResponse(message=Message(role="assistant", content=f"result: {last['content']}"))
        if isinstance(last, dict) and "points" in last["content"]:
            call = Message.ToolCall(
                function=Message.ToolCall.Function(name="get_unity_balance", arguments={"member_id": "HR-100245"})
            )
            return ChatResponse(message=Message(role="assistant", tool_calls=[call]))
        system = history[0]
        turns = sum(1 for m in history if isinstance(m, dict) and m.get("role") == "user")
        assert isinstance(system, dict)
        return ChatResponse(message=Message(role="assistant", content=f"{system['content']} turn {turns}"))


class TokenEchoTools:
    """Answers tool calls with the user token the conversation holds at that moment."""

    def __init__(self) -> None:
        self.user_token: str | None = None

    def ollama_tools(self) -> list[dict[str, Any]]:
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        return f"token={self.user_token}"


def make_client(llm: EchoLlm | None = None) -> TestClient:
    def new_conversation(guest: Guest, observer: ToolCallObserver) -> tuple[Agent, TokenEchoTools]:
        tools = TokenEchoTools()
        return Agent(llm or EchoLlm(), tools, "m", system_prompt=guest.member_id, on_tool_call=observer), tools

    runtime = Runtime(model="m", tool_names=["get_unity_balance"], new_conversation=new_conversation)
    return TestClient(create_app(runtime, MEMBERS))


def test_health_is_open() -> None:
    assert make_client().get("/health").json() == {
        "status": "ok",
        "model": "m",
        "tools": ["get_unity_balance"],
        "conversations": 0,
    }


def test_serves_no_chat_page() -> None:
    assert make_client().get("/").status_code == 404


def test_chat_requires_a_forwarded_token() -> None:
    response = make_client().post("/chat", json={"message": "hi"})
    assert response.status_code == 401
    assert "Bearer" in response.json()["error"]


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Authorization": "Bearer not-a-jwt"}, 401),
        ({"Authorization": f"Bearer {token_for('mallory')}"}, 403),
    ],
)
def test_chat_rejects_tokens_that_name_no_member(headers: dict[str, str], status: int) -> None:
    assert make_client().post("/chat", json={"message": "hi"}, headers=headers).status_code == status


def test_conversation_is_bound_to_the_tokens_member() -> None:
    client = make_client()
    first = client.post("/chat", json={"message": "hi"}, headers=auth("priya")).json()
    assert first == {"reply": "HR-100377 turn 1", "tool_calls": [], "member_id": "HR-100377"}
    assert client.post("/chat", json={"message": "again"}, headers=auth("priya")).json()["reply"] == "HR-100377 turn 2"


def test_gateway_forwarded_header_also_works() -> None:
    headers = {"X-Forwarded-Authorization": f"Bearer {token_for('alex')}"}
    assert make_client().post("/chat", json={"message": "hi"}, headers=headers).json()["member_id"] == "HR-100245"


def test_tool_calls_see_the_latest_user_token() -> None:
    client = make_client()
    fresh = auth("alex")
    body = client.post("/chat", json={"message": "my points?"}, headers=fresh).json()
    token = fresh["Authorization"].removeprefix("Bearer ")
    assert body["tool_calls"] == [
        {"name": "get_unity_balance", "arguments": {"member_id": "HR-100245"}, "result": f"token={token}"}
    ]


def test_conversation_ids_are_scoped_per_guest() -> None:
    client = make_client()
    client.post("/chat", json={"message": "a", "conversation_id": "c1"}, headers=auth("alex"))
    # Same conversation id, different guest: a fresh conversation, not alex's history.
    other = client.post("/chat", json={"message": "b", "conversation_id": "c1"}, headers=auth("priya"))
    assert other.json()["reply"] == "HR-100377 turn 1"
    again = client.post("/chat", json={"message": "c", "conversation_id": "c1"}, headers=auth("alex"))
    assert again.json()["reply"] == "HR-100245 turn 2"
    new = client.post("/chat", json={"message": "d", "conversation_id": "c2"}, headers=auth("alex"))
    assert new.json()["reply"] == "HR-100245 turn 1"


@pytest.mark.parametrize(
    ("kwargs", "status", "error"),
    [
        ({"content": "not json", "headers": {"content-type": "application/json"}}, 400, "must be JSON"),
        ({"json": [1]}, 400, "JSON object"),
        ({"json": {"message": ""}}, 400, "non-empty string"),
        ({"json": {"message": "hi", "conversation_id": 5}}, 400, "'conversation_id' must be a string"),
        ({"data": {"message": "hi"}}, 415, "application/json"),
    ],
)
def test_chat_rejects_bad_requests(kwargs: dict[str, Any], status: int, error: str) -> None:
    headers = {**auth(), **kwargs.pop("headers", {})}
    response = make_client().post("/chat", headers=headers, **kwargs)
    assert response.status_code == status
    assert error in response.json()["error"]


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (ResponseError("model 'x' not found", 404), 502),
        (ConnectionError("refused"), 502),
        (AgentStepLimitError("no final answer"), 500),
    ],
)
def test_chat_maps_agent_failures(exc: Exception, status: int) -> None:
    assert make_client(EchoLlm(error=exc)).post("/chat", json={"message": "hi"}, headers=auth()).status_code == status
