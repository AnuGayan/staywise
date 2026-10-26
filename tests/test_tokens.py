import base64
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest

from concierge.tokens import (
    TOKEN_EXCHANGE,
    TOOL_SCOPES,
    AgentIdentity,
    GuestTools,
    TokenBroker,
    TokenConfigError,
    TokenError,
)

IDENTITY = AgentIdentity("agent-client", "agent-secret", "http://thunder.svc:8090/oauth2/token")


class FakeThunderTokens:
    """A token endpoint that grants client_credentials and exchanges, narrowing to the subject's scopes."""

    def __init__(self, user_scopes: str = "hotel:read bookings:read unity:read") -> None:
        self.user_scopes = set(user_scopes.split())
        self.forms: list[dict[str, str]] = []
        self.auth_headers: list[str] = []
        self.fail_with: tuple[int, dict[str, str]] | None = None
        self.scope_in_body = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        self.forms.append(form)
        self.auth_headers.append(request.headers.get("authorization", ""))
        if self.fail_with:
            status, body = self.fail_with
            return httpx.Response(status, json=body)
        if form["grant_type"] == "client_credentials":
            return httpx.Response(200, json={"access_token": "agent-at", "expires_in": 3600})
        granted = " ".join(sorted(set(form["scope"].split()) & self.user_scopes))
        token = jwt.encode({"scope": granted, "act": {"sub": "agent"}} if granted else {}, "k" * 32, "HS256")
        body: dict[str, Any] = {"access_token": token, "expires_in": 3600}
        if self.scope_in_body and granted:
            body["scope"] = granted
        return httpx.Response(200, json=body)


def broker_for(thunder: FakeThunderTokens, clock: list[float] | None = None) -> TokenBroker:
    now = clock if clock is not None else [1000.0]
    http = httpx.AsyncClient(transport=httpx.MockTransport(thunder.handler))
    return TokenBroker(IDENTITY, http, clock=lambda: now[0])


def test_identity_from_env_is_optional_but_must_be_complete() -> None:
    assert AgentIdentity.from_env({}) is None
    identity = AgentIdentity.from_env(
        {
            "AMP_AGENTID_CLIENT_ID": "id",
            "AMP_AGENTID_CLIENT_SECRET": "secret",
            "AMP_AGENTID_TOKEN_ENDPOINT": "http://t/oauth2/token",
        }
    )
    assert identity == AgentIdentity("id", "secret", "http://t/oauth2/token", "urn:hardrock:concierge-api")
    with pytest.raises(TokenConfigError, match="AMP_AGENTID_CLIENT_SECRET, AMP_AGENTID_TOKEN_ENDPOINT"):
        AgentIdentity.from_env({"AMP_AGENTID_CLIENT_ID": "id"})


async def test_exchange_sends_user_as_subject_and_agent_as_actor() -> None:
    thunder = FakeThunderTokens()
    token = await broker_for(thunder).for_guest("user-at", "unity:read")
    assert jwt.decode(token, options={"verify_signature": False})["scope"] == "unity:read"
    agent_form, exchange_form = thunder.forms
    assert agent_form == {"grant_type": "client_credentials", "resource": "urn:hardrock:concierge-api"}
    assert exchange_form["grant_type"] == TOKEN_EXCHANGE
    assert (exchange_form["subject_token"], exchange_form["actor_token"]) == ("user-at", "agent-at")
    assert (exchange_form["scope"], exchange_form["resource"]) == ("unity:read", "urn:hardrock:concierge-api")
    assert set(thunder.auth_headers) == {"Basic " + base64.b64encode(b"agent-client:agent-secret").decode()}


async def test_tokens_are_cached_per_user_and_scope_until_near_expiry() -> None:
    thunder = FakeThunderTokens()
    now = [1000.0]
    broker = broker_for(thunder, now)
    first = await broker.for_guest("user-at", "hotel:read")
    assert await broker.for_guest("user-at", "hotel:read") == first
    await broker.for_guest("user-at", "unity:read")
    await broker.for_guest("other-user-at", "hotel:read")
    assert [f["grant_type"] for f in thunder.forms].count(TOKEN_EXCHANGE) == 3
    assert [f["grant_type"] for f in thunder.forms].count("client_credentials") == 1
    now[0] += 3600 - 59  # inside the 60 s safety margin
    await broker.for_guest("user-at", "hotel:read")
    assert [f["grant_type"] for f in thunder.forms].count(TOKEN_EXCHANGE) == 4


@pytest.mark.parametrize("scope_in_body", [True, False])
async def test_a_scope_the_guest_lacks_is_refused(scope_in_body: bool) -> None:
    thunder = FakeThunderTokens(user_scopes="hotel:read")
    thunder.scope_in_body = scope_in_body
    with pytest.raises(TokenError, match="does not allow bookings:write"):
        await broker_for(thunder).for_guest("user-at", "bookings:write")


async def test_thunder_errors_become_token_errors() -> None:
    thunder = FakeThunderTokens()
    thunder.fail_with = (400, {"error": "unauthorized_client", "error_description": "token exchange not allowed"})
    with pytest.raises(TokenError, match="refused the agent token: token exchange not allowed"):
        await broker_for(thunder).for_guest("user-at", "hotel:read")


async def test_unreachable_thunder_is_a_token_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    broker = TokenBroker(IDENTITY, httpx.AsyncClient(transport=httpx.MockTransport(down)))
    with pytest.raises(TokenError, match="cannot reach ThunderID"):
        await broker.for_guest("user-at", "hotel:read")


class RecordingHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def ollama_tools(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {"name": "get_unity_balance"}}]

    async def call_tool(self, name: str, arguments: dict[str, Any], bearer: str | None = None) -> str:
        self.calls.append((name, bearer))
        return "ok"


async def test_guest_tools_attach_a_scoped_token_per_call() -> None:
    hub = RecordingHub()
    tools = GuestTools(hub, broker_for(FakeThunderTokens()))
    tools.user_token = "user-at"
    assert await tools.call_tool("get_unity_balance", {"member_id": "HR-100245"}) == "ok"
    name, bearer = hub.calls[0]
    assert name == "get_unity_balance"
    assert bearer is not None
    assert jwt.decode(bearer, options={"verify_signature": False})["scope"] == TOOL_SCOPES["get_unity_balance"]
    assert tools.ollama_tools() == hub.ollama_tools()


async def test_guest_tools_report_refusals_to_the_model_without_calling() -> None:
    hub = RecordingHub()
    tools = GuestTools(hub, broker_for(FakeThunderTokens(user_scopes="hotel:read")))
    tools.user_token = "user-at"
    result = await tools.call_tool("create_booking", {})
    assert result == "Error: not authorized to use create_booking: the guest's sign-in does not allow bookings:write"
    assert hub.calls == []


@pytest.mark.parametrize(("user_token", "tool"), [(None, "get_unity_balance"), ("user-at", "unmapped_tool")])
async def test_guest_tools_fall_back_to_the_agents_own_token(user_token: str | None, tool: str) -> None:
    hub = RecordingHub()
    thunder = FakeThunderTokens()
    tools = GuestTools(hub, broker_for(thunder))
    tools.user_token = user_token
    await tools.call_tool(tool, {})
    assert hub.calls == [(tool, "agent-at")]
    assert [form["grant_type"] for form in thunder.forms] == ["client_credentials"]


async def test_guest_tools_without_a_broker_send_no_token() -> None:
    hub = RecordingHub()
    tools = GuestTools(hub, None)
    tools.user_token = "user-at"
    await tools.call_tool("get_unity_balance", {})
    assert hub.calls == [("get_unity_balance", None)]


async def test_agent_token_refusal_reaches_the_model() -> None:
    thunder = FakeThunderTokens()
    thunder.fail_with = (401, {"error": "invalid_client"})
    tools = GuestTools(RecordingHub(), broker_for(thunder))
    assert await tools.call_tool("unmapped_tool", {}) == (
        "Error: not authorized to use unmapped_tool: ThunderID refused the agent token: invalid_client"
    )


def test_every_server_tool_has_a_scope() -> None:
    assert set(TOOL_SCOPES) == {
        "get_hotel_info",
        "list_room_types",
        "check_availability",
        "list_bookings",
        "get_booking",
        "create_booking",
        "cancel_booking",
        "get_unity_balance",
        "get_points_activity",
    }
