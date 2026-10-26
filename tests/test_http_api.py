from collections.abc import Mapping, Sequence
from typing import Any

import httpx
import pytest
from ollama import ChatResponse, Message, ResponseError
from starlette.testclient import TestClient

from concierge.agent import Agent, AgentStepLimitError, ToolCallObserver
from concierge.auth import SignedInGuest, TokenGrant, WebAuth
from concierge.http_api import SESSION_COOKIE, Runtime, create_app

USERS = {"alex": "HR-100245", "priya": "HR-100377"}


class EchoLlm:
    """Calls `get_unity_balance` when asked about points; otherwise echoes the system prompt's member id."""

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


class FakeTools:
    def ollama_tools(self) -> list[dict[str, Any]]:
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        return "18450 points"


class FakeIdp:
    """Signs in whichever username the test queued for the next code."""

    def __init__(self) -> None:
        self.username = "alex"
        self.last_state = ""

    async def authorization_url(self, state: str, nonce: str, code_challenge: str) -> str:
        self.last_state = state
        return f"http://idp.test/oauth2/authorize?state={state}"

    async def exchange_code(self, code: str, code_verifier: str) -> TokenGrant:
        return TokenGrant(id_token=code, expires_in=3600)

    async def verify_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        return {"sub": f"sub-{self.username}", "username": self.username, "name": self.username.title()}


def make_client(llm: EchoLlm | None = None) -> tuple[TestClient, FakeIdp]:
    def new_agent(guest: SignedInGuest, observer: ToolCallObserver) -> Agent:
        return Agent(llm or EchoLlm(), FakeTools(), "m", system_prompt=guest.member_id, on_tool_call=observer)

    idp = FakeIdp()
    app = create_app(Runtime(model="m", tool_names=["get_unity_balance"], new_agent=new_agent), WebAuth(idp, USERS))
    return TestClient(app, base_url="http://localhost:9100", follow_redirects=False), idp


def sign_in(client: TestClient, idp: FakeIdp, username: str = "alex") -> None:
    idp.username = username
    assert client.get("/login").status_code == 303
    response = client.get("/auth/callback", params={"state": idp.last_state, "code": "c"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_health_is_public() -> None:
    client, _ = make_client()
    assert client.get("/health").json() == {"status": "ok", "model": "m", "tools": ["get_unity_balance"], "sessions": 0}


@pytest.mark.parametrize(("method", "path"), [("GET", "/me"), ("POST", "/chat"), ("DELETE", "/chat")])
def test_guest_endpoints_require_sign_in(method: str, path: str) -> None:
    client, _ = make_client()
    response = client.request(method, path, json={"message": "hi"} if method == "POST" else None)
    assert response.status_code == 401


def test_chat_page_redirects_to_login_until_signed_in() -> None:
    client, idp = make_client()
    assert client.get("/").headers["location"] == "/login"
    login = client.get("/login")
    assert login.headers["location"].startswith("http://idp.test/oauth2/authorize?state=")
    sign_in(client, idp)
    page = client.get("/")
    assert page.status_code == 200
    assert "<title>Hard Rock Miami Concierge</title>" in page.text


def test_session_cookie_is_http_only_and_lax() -> None:
    client, idp = make_client()
    client.get("/login")
    response = client.get("/auth/callback", params={"state": idp.last_state, "code": "c"})
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=3600" in cookie


def test_me_reports_the_signed_in_guest() -> None:
    client, idp = make_client()
    sign_in(client, idp, "priya")
    assert client.get("/me").json() == {"username": "priya", "name": "Priya", "member_id": "HR-100377"}


def test_conversation_is_bound_to_the_guests_member_id() -> None:
    client, idp = make_client()
    sign_in(client, idp, "priya")
    assert client.post("/chat", json={"message": "hi"}).json() == {"reply": "HR-100377 turn 1", "tool_calls": []}
    assert client.post("/chat", json={"message": "again"}).json()["reply"] == "HR-100377 turn 2"


def test_chat_returns_tool_calls() -> None:
    client, idp = make_client()
    sign_in(client, idp)
    body = client.post("/chat", json={"message": "my points?"}).json()
    assert body == {
        "reply": "result: 18450 points",
        "tool_calls": [
            {"name": "get_unity_balance", "arguments": {"member_id": "HR-100245"}, "result": "18450 points"}
        ],
    }


def test_each_browser_gets_its_own_conversation() -> None:
    first, idp_one = make_client()
    sign_in(first, idp_one, "alex")
    # A second browser against the same app: share the app, not the cookie jar.
    second = TestClient(first.app, base_url="http://localhost:9100", follow_redirects=False)
    sign_in(second, idp_one, "priya")
    assert first.post("/chat", json={"message": "a"}).json()["reply"] == "HR-100245 turn 1"
    assert second.post("/chat", json={"message": "b"}).json()["reply"] == "HR-100377 turn 1"


def test_delete_chat_starts_over_and_logout_ends_the_session() -> None:
    client, idp = make_client()
    sign_in(client, idp)
    client.post("/chat", json={"message": "one"})
    assert client.delete("/chat").status_code == 204
    assert client.post("/chat", json={"message": "two"}).json()["reply"] == "HR-100245 turn 1"
    assert client.post("/logout").status_code == 204
    assert client.get("/me").status_code == 401


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"error": "access_denied", "error_description": "user <b>cancelled</b>"}, "user &lt;b&gt;cancelled&lt;/b&gt;"),
        ({"state": "x"}, "missing its code or state"),
        ({"state": "forged", "code": "c"}, "expired or was already used"),
    ],
)
def test_callback_failures_show_an_escaped_message(params: dict[str, str], message: str) -> None:
    client, _ = make_client()
    response = client.get("/auth/callback", params=params)
    assert response.status_code == 400
    assert message in response.text
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")


def test_unlinked_account_cannot_sign_in() -> None:
    client, idp = make_client()
    idp.username = "mallory"
    client.get("/login")
    response = client.get("/auth/callback", params={"state": idp.last_state, "code": "c"})
    assert response.status_code == 400
    assert "not linked to a Unity" in response.text


@pytest.mark.parametrize(
    ("kwargs", "status", "error"),
    [
        ({"content": "not json", "headers": {"content-type": "application/json"}}, 400, "must be JSON"),
        ({"json": [1]}, 400, "JSON object"),
        ({"json": {"message": ""}}, 400, "non-empty string"),
        ({"data": {"message": "hi"}}, 415, "application/json"),
    ],
)
def test_chat_rejects_bad_requests(kwargs: dict[str, Any], status: int, error: str) -> None:
    client, idp = make_client()
    sign_in(client, idp)
    response = client.post("/chat", **kwargs)
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
    client, idp = make_client(EchoLlm(error=exc))
    sign_in(client, idp)
    assert client.post("/chat", json={"message": "hi"}).status_code == status


class DownIdp(FakeIdp):
    async def authorization_url(self, state: str, nonce: str, code_challenge: str) -> str:
        raise httpx.ConnectError("connection refused")


def test_login_reports_an_unreachable_thunder() -> None:
    def new_agent(guest: SignedInGuest, observer: ToolCallObserver) -> Agent:
        return Agent(EchoLlm(), FakeTools(), "m", on_tool_call=observer)

    app = create_app(Runtime(model="m", tool_names=[], new_agent=new_agent), WebAuth(DownIdp(), USERS))
    response = TestClient(app, follow_redirects=False).get("/login")
    assert response.status_code == 502
    assert "cannot reach ThunderID" in response.text
