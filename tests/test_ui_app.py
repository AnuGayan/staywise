import json
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from concierge.auth import TokenGrant, WebAuth
from concierge.ui_app import SESSION_COOKIE, create_app
from concierge.ui_main import agent_headers, parse_args


class FakeIdp:
    """Signs in whichever username the test queued; the access token is `at-<username>`."""

    def __init__(self) -> None:
        self.username = "alex"
        self.last_state = ""

    async def authorization_url(self, state: str, nonce: str, code_challenge: str) -> str:
        self.last_state = state
        return f"http://idp.test/oauth2/authorize?state={state}"

    async def exchange_code(self, code: str, code_verifier: str) -> TokenGrant:
        return TokenGrant(id_token=code, access_token=f"at-{self.username}", expires_in=3600)

    async def verify_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        return {"sub": f"sub-{self.username}", "username": self.username, "name": self.username.title()}


class FakeAgent:
    """The agent behind the gateway: records what it received and answers with a canned status/body."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: Any = {"reply": "hello", "tool_calls": [], "member_id": "HR-100245"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.body, str):
            return httpx.Response(self.status, text=self.body)
        return httpx.Response(self.status, json=self.body)


def make_client(
    agent: FakeAgent | None = None, base_path: str = "", headers: dict[str, str] | None = None
) -> tuple[TestClient, FakeIdp, FakeAgent]:
    idp, fake_agent = FakeIdp(), agent or FakeAgent()
    agent_http = httpx.AsyncClient(base_url="http://gw.test/melody/", transport=httpx.MockTransport(fake_agent.handler))
    app = create_app(WebAuth(idp), agent_http, agent_headers=headers, base_path=base_path)
    return TestClient(app, base_url="http://localhost:9100", follow_redirects=False), idp, fake_agent


def sign_in(client: TestClient, idp: FakeIdp, username: str = "alex") -> None:
    idp.username = username
    assert client.get("/login").status_code == 303
    response = client.get("/auth/callback", params={"state": idp.last_state, "code": "c"})
    assert response.status_code == 303


def test_health_names_the_agent() -> None:
    client, _, _ = make_client()
    assert client.get("/health").json() == {"status": "ok", "agent": "http://gw.test/melody/"}


@pytest.mark.parametrize(("method", "path"), [("GET", "/me"), ("POST", "/chat"), ("DELETE", "/chat")])
def test_guest_endpoints_require_sign_in(method: str, path: str) -> None:
    client, _, _ = make_client()
    response = client.request(method, path, json={"message": "hi"} if method == "POST" else None)
    assert response.status_code == 401


def test_chat_page_redirects_to_login_until_signed_in() -> None:
    client, idp, _ = make_client()
    assert client.get("/").headers["location"] == "/login"
    assert client.get("/login").headers["location"].startswith("http://idp.test/oauth2/authorize?state=")
    sign_in(client, idp)
    page = client.get("/")
    assert page.status_code == 200
    assert "<title>Hard Rock Miami Concierge</title>" in page.text


def test_session_cookie_is_http_only_and_lax() -> None:
    client, idp, _ = make_client()
    client.get("/login")
    response = client.get("/auth/callback", params={"state": idp.last_state, "code": "c"})
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=3600" in cookie


def test_me_shows_the_user_but_never_the_token() -> None:
    client, idp, _ = make_client()
    sign_in(client, idp, "priya")
    assert client.get("/me").json() == {"username": "priya", "name": "Priya"}


def test_chat_is_forwarded_with_the_users_bearer_token() -> None:
    client, idp, agent = make_client(headers={"X-API-Key": "gw-key"})
    sign_in(client, idp)
    response = client.post("/chat", json={"message": "hi"})
    assert response.status_code == 200
    assert response.json() == {"reply": "hello", "tool_calls": [], "member_id": "HR-100245"}
    sent = agent.requests[0]
    assert str(sent.url) == "http://gw.test/melody/chat"
    assert sent.headers["authorization"] == "Bearer at-alex"
    assert sent.headers["x-api-key"] == "gw-key"
    body = json.loads(sent.content)
    assert body["message"] == "hi"
    assert len(body["conversation_id"]) == 32


def test_conversation_id_is_stable_until_a_new_chat() -> None:
    client, idp, agent = make_client()
    sign_in(client, idp)
    client.post("/chat", json={"message": "one"})
    client.post("/chat", json={"message": "two"})
    assert client.delete("/chat").status_code == 204
    client.post("/chat", json={"message": "three"})
    ids = [json.loads(r.content)["conversation_id"] for r in agent.requests]
    assert ids[0] == ids[1] != ids[2]


def test_agent_401_ends_the_session() -> None:
    agent = FakeAgent()
    agent.status, agent.body = 401, {"error": "Unauthorized"}
    client, idp, _ = make_client(agent)
    sign_in(client, idp)
    response = client.post("/chat", json={"message": "hi"})
    assert response.status_code == 401
    assert "sign in again" in response.json()["error"]
    assert client.get("/me").status_code == 401


def test_agent_errors_pass_through_and_non_json_becomes_502() -> None:
    agent = FakeAgent()
    agent.status, agent.body = 403, {"error": "not linked to a Unity membership"}
    client, idp, _ = make_client(agent)
    sign_in(client, idp)
    response = client.post("/chat", json={"message": "hi"})
    assert (response.status_code, response.json()) == (403, {"error": "not linked to a Unity membership"})
    agent.status, agent.body = 503, "upstream connect error"
    response = client.post("/chat", json={"message": "hi"})
    assert response.status_code == 502
    assert "HTTP 503: upstream connect error" in response.json()["error"]


def test_unreachable_agent_is_a_502() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    idp = FakeIdp()
    app = create_app(WebAuth(idp), httpx.AsyncClient(base_url="http://gw.test/", transport=httpx.MockTransport(down)))
    client = TestClient(app, follow_redirects=False)
    sign_in(client, idp)
    response = client.post("/chat", json={"message": "hi"})
    assert response.status_code == 502
    assert "cannot reach the agent" in response.json()["error"]


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [({"json": {"message": ""}}, 400), ({"data": {"message": "hi"}}, 415), ({"json": [1]}, 400)],
)
def test_chat_validates_before_forwarding(kwargs: dict[str, Any], status: int) -> None:
    client, idp, agent = make_client()
    sign_in(client, idp)
    assert client.post("/chat", **kwargs).status_code == status
    assert agent.requests == []


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"error": "access_denied", "error_description": "user <b>cancelled</b>"}, "user &lt;b&gt;cancelled&lt;/b&gt;"),
        ({"state": "x"}, "missing its code or state"),
        ({"state": "forged", "code": "c"}, "expired or was already used"),
    ],
)
def test_callback_failures_show_an_escaped_message(params: dict[str, str], message: str) -> None:
    client, _, _ = make_client()
    response = client.get("/auth/callback", params=params)
    assert response.status_code == 400
    assert message in response.text
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")


def test_redirects_stay_under_a_path_prefix() -> None:
    client, idp, _ = make_client(base_path="/concierge")
    assert client.get("/").headers["location"] == "/concierge/login"
    client.get("/login")
    signed_in = client.get("/auth/callback", params={"state": idp.last_state, "code": "c"})
    assert signed_in.headers["location"] == "/concierge/"
    failed = client.get("/auth/callback", params={"state": "forged", "code": "c"})
    assert "href='/concierge/login'" in failed.text


def test_ui_flags_and_optional_agent_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_URL", raising=False)
    monkeypatch.delenv("UI_PORT", raising=False)
    args = parse_args([])
    assert (args.agent_url, args.port) == ("http://default-default.am-gateway.localhost:19080/melody", 9100)
    assert agent_headers({}) == {}
    assert agent_headers({"AGENT_API_KEY": " k "}) == {"API-Key": "k"}
    assert agent_headers({"AGENT_API_KEY": "k", "AGENT_API_KEY_HEADER": "API-Key"}) == {"API-Key": "k"}
