"""Tokens the agent gets from ThunderID to call MCP servers on the guest's behalf.

Agent Manager gives the agent its own identity (AMP_AGENTID_CLIENT_ID / _SECRET / _TOKEN_ENDPOINT).
For each tool call the agent exchanges the guest's token (subject) plus its own token (actor) for a
token limited to the one scope that tool needs (RFC 8693). ThunderID only ever narrows scopes, so a
guest can never get more than they signed in with.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from opentelemetry import trace

from concierge.identity import unverified_claims
from concierge.telemetry import tracer

DEFAULT_RESOURCE = "urn:hardrock:concierge-api"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
EXPIRY_MARGIN_SECONDS = 60
MAX_CACHED_TOKENS = 500

# The scope each MCP tool needs; the same table the gateway's mcp-authz rules will enforce.
TOOL_SCOPES: Mapping[str, str] = {
    "get_hotel_info": "hotel:read",
    "list_room_types": "hotel:read",
    "check_availability": "bookings:read",
    "list_bookings": "bookings:read",
    "get_booking": "bookings:read",
    "create_booking": "bookings:write",
    "cancel_booking": "bookings:write",
    "get_unity_balance": "unity:read",
    "get_points_activity": "unity:read",
}


class TokenConfigError(ValueError):
    """The agent identity variables are only partly set."""


class TokenError(Exception):
    """ThunderID would not issue a token for a tool call. The message is safe to show the model."""


@dataclass(frozen=True)
class AgentIdentity:
    """The agent's own OAuth client, as Agent Manager provides it."""

    client_id: str
    client_secret: str
    token_endpoint: str
    resource: str = DEFAULT_RESOURCE

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> AgentIdentity | None:
        """Read AMP_AGENTID_*; None when none are set (e.g. running locally without Agent Manager).

        Raises:
            TokenConfigError: if only some of the three variables are set.
        """
        names = ("AMP_AGENTID_CLIENT_ID", "AMP_AGENTID_CLIENT_SECRET", "AMP_AGENTID_TOKEN_ENDPOINT")
        values = [environ.get(name, "").strip() for name in names]
        if not any(values):
            return None
        missing = [name for name, value in zip(names, values, strict=True) if not value]
        if missing:
            raise TokenConfigError(f"agent identity is incomplete: {', '.join(missing)} not set")
        client_id, client_secret, token_endpoint = values
        return cls(client_id, client_secret, token_endpoint, environ.get("THUNDER_RESOURCE", DEFAULT_RESOURCE))


@dataclass(frozen=True)
class _Cached:
    token: str
    expires_at: float


class TokenBroker:
    """Gets and caches the agent's own token and per-scope exchanged tokens."""

    def __init__(self, identity: AgentIdentity, http: httpx.AsyncClient, clock: Callable[[], float] = time.time):
        self._identity = identity
        self._http = http
        self._clock = clock
        self._agent: _Cached | None = None
        self._exchanged: OrderedDict[tuple[str, str], _Cached] = OrderedDict()

    async def for_guest(self, user_token: str, scope: str) -> str:
        """A token acting for the guest, limited to `scope`, from the token-exchange grant.

        Raises:
            TokenError: if ThunderID refuses the exchange or doesn't grant `scope`.
        """
        key = (hashlib.sha256(user_token.encode()).hexdigest(), scope)
        attributes = {"oauth.grant_type": TOKEN_EXCHANGE, "oauth.scope": scope}
        with tracer.start_as_current_span("token_exchange", attributes=attributes) as span:
            cached = self._exchanged.get(key)
            span.set_attribute("oauth.token.cached", bool(cached and self._fresh(cached)))
            if cached and self._fresh(cached):
                return cached.token
            body = await self._token_request(
                {
                    "grant_type": TOKEN_EXCHANGE,
                    "subject_token": user_token,
                    "subject_token_type": ACCESS_TOKEN_TYPE,
                    "actor_token": await self.agent_token(),
                    "actor_token_type": ACCESS_TOKEN_TYPE,
                    "requested_token_type": ACCESS_TOKEN_TYPE,
                    "resource": self._identity.resource,
                    "scope": scope,
                },
                "token exchange",
            )
            token = body["access_token"]
            if scope not in _granted_scopes(body, token):
                raise TokenError(f"the guest's sign-in does not allow {scope}")
        self._exchanged[key] = self._cache(token, body)
        while len(self._exchanged) > MAX_CACHED_TOKENS:
            self._exchanged.popitem(last=False)
        return token

    async def agent_token(self) -> str:
        """The agent's own token (client_credentials), cached until shortly before it expires.

        Raises:
            TokenError: if ThunderID refuses or can't be reached.
        """
        with tracer.start_as_current_span("agent_token", attributes={"oauth.grant_type": "client_credentials"}) as span:
            fresh = self._agent is not None and self._fresh(self._agent)
            span.set_attribute("oauth.token.cached", fresh)
            if self._agent is not None and fresh:
                return self._agent.token
            body = await self._token_request(
                {"grant_type": "client_credentials", "resource": self._identity.resource}, "agent token"
            )
            self._agent = self._cache(body["access_token"], body)
            return self._agent.token

    async def _token_request(self, form: dict[str, str], purpose: str) -> dict[str, Any]:
        try:
            response = await self._http.post(
                self._identity.token_endpoint,
                data=form,
                auth=(self._identity.client_id, self._identity.client_secret),
            )
        except httpx.HTTPError as exc:
            raise TokenError(f"cannot reach ThunderID for the {purpose}: {exc}") from exc
        trace.get_current_span().set_attribute("http.response.status_code", response.status_code)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != httpx.codes.OK or not isinstance(body.get("access_token"), str):
            detail = body.get("error_description") or body.get("error") or f"HTTP {response.status_code}"
            raise TokenError(f"ThunderID refused the {purpose}: {detail}")
        return body

    def _cache(self, token: str, body: Mapping[str, Any]) -> _Cached:
        return _Cached(token, self._clock() + int(body.get("expires_in", 300)))

    def _fresh(self, cached: _Cached) -> bool:
        return self._clock() < cached.expires_at - EXPIRY_MARGIN_SECONDS


def _granted_scopes(body: Mapping[str, Any], token: str) -> set[str]:
    """Scopes from the token response, falling back to the token's own `scope` claim."""
    raw = body.get("scope")
    if not isinstance(raw, str):
        raw = str(unverified_claims(token).get("scope", ""))
    return set(raw.split())


class ToolCaller(Protocol):
    """The slice of `McpHub` used here."""

    def ollama_tools(self) -> list[dict[str, Any]]:
        """Tool schemas in Ollama's function-calling format."""
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any], bearer: str | None = None) -> str:
        """Run a tool, optionally with a bearer token."""
        ...


class GuestTools:
    """The hub's tools as one guest's conversation sees them: each call carries a token minted for it.

    Tools in the scope map get a token exchanged for the guest; anything else, or a turn without a
    guest token, uses the agent's own token. Without a broker (no agent identity, e.g. locally)
    calls carry no token.
    """

    def __init__(self, hub: ToolCaller, broker: TokenBroker | None, scopes: Mapping[str, str] = TOOL_SCOPES):
        self._hub = hub
        self._broker = broker
        self._scopes = scopes
        self.user_token: str | None = None

    def ollama_tools(self) -> list[dict[str, Any]]:
        """Tool schemas in Ollama's function-calling format."""
        return self._hub.ollama_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Run a tool for the guest. A refused token comes back as an "Error: ..." the model can explain."""
        # Recorded on the caller's span (the agent's `execute_tool` span): which credential this call used.
        span = trace.get_current_span()
        if self._broker is None:
            span.set_attribute("melody.tool.credential", "none")
            return await self._hub.call_tool(name, arguments)
        scope = self._scopes.get(name)
        if scope is not None:
            span.set_attribute("melody.tool.scope", scope)
        try:
            if scope is not None and self.user_token:
                span.set_attribute("melody.tool.credential", "guest_exchanged")
                token = await self._broker.for_guest(self.user_token, scope)
            else:
                span.set_attribute("melody.tool.credential", "agent")
                token = await self._broker.agent_token()
        except TokenError as exc:
            return f"Error: not authorized to use {name}: {exc}"
        return await self._hub.call_tool(name, arguments, bearer=token)
