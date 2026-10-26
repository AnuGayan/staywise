"""Test doubles: a Thunder that mints tokens with the test key, a scripted LLM, and in-memory MCP servers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from ollama import ChatResponse, Message

from servers import catalog_server, concierge_server, payments_server, reservations_server
from servers.runner import GuardSettings, guarded_app
from servers.store import HotelStore
from staywise.auth.policy import StepUpPolicy, load_policy
from staywise.auth.thunder import ClientCredentials, OAuthError, ThunderClient, TokenSet, peek_claims
from staywise.config import ServerConfig
from staywise.settings import Settings
from tests.conftest import AGENT_ID, GUEST, ISSUER, RESOURCE, STEPUP_CLIENT, WEB_CLIENT, StaticKeyVerifier, make_token

ROOT = Path(__file__).resolve().parent.parent
SERVER_MODULES = {
    "catalog": (catalog_server, 8101),
    "reservations": (reservations_server, 8102),
    "payments": (payments_server, 8103),
    "concierge": (concierge_server, 8104),
}

WEB = ClientCredentials(WEB_CLIENT, "web-secret")
STEPUP = ClientCredentials(STEPUP_CLIENT, "stepup-secret")
AGENT = ClientCredentials(AGENT_ID, "agent-secret")


def token_set(scopes: str, **claims: Any) -> TokenSet:
    access = make_token(scopes, **claims)
    payload = peek_claims(access)
    return TokenSet(access, float(payload["exp"]), frozenset(scopes.split()), "refresh-1", "id-token", payload)


@dataclass
class FakeThunder(ThunderClient):
    """Behaves like Thunder 1.0.0 where it matters: exchange only narrows, and adds `act`."""

    calls: list[tuple[str, frozenset[str]]] = field(default_factory=list)
    agent_scopes: frozenset[str] = frozenset({"ops:notify"})
    refuse: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        super().__init__(ISSUER, RESOURCE)

    async def client_credentials(self, client: ClientCredentials, scopes: frozenset[str]) -> TokenSet:
        self.calls.append(("client_credentials", scopes))
        granted = " ".join(sorted(scopes & self.agent_scopes))
        return token_set(granted, sub=client.client_id, client_id=client.client_id)

    async def token_exchange(
        self, client: ClientCredentials, subject_token: str, scopes: frozenset[str], actor_token: str | None = None
    ) -> TokenSet:
        self.calls.append(("token_exchange", scopes))
        if "token_exchange" in self.refuse:
            raise OAuthError("invalid_grant", "refused by test")
        subject = peek_claims(subject_token)
        granted = scopes & frozenset(str(subject.get("scope", "")).split())
        act = {"sub": peek_claims(actor_token)["sub"]} if actor_token else None
        return token_set(" ".join(sorted(granted)), sub=subject["sub"], client_id=client.client_id, act=act)

    async def refresh(self, client: ClientCredentials, refresh_token: str) -> TokenSet:
        self.calls.append(("refresh", frozenset()))
        return token_set(LOGIN_SCOPES, client_id=client.client_id)

    async def exchange_code(self, client: ClientCredentials, code: str, redirect_uri: str, verifier: str) -> TokenSet:
        self.calls.append(("authorization_code", frozenset()))
        if code == "bad":
            raise OAuthError("invalid_grant", "bad code")
        if client.client_id == STEPUP_CLIENT:
            sub = "someone-else" if code == "other-user" else GUEST
            return token_set("openid payment:highvalue", sub=sub, client_id=STEPUP_CLIENT, ttl=300)
        return token_set(LOGIN_SCOPES, client_id=client.client_id)

    async def verify_id_token(self, id_token: str, client_id: str, nonce: str) -> dict[str, Any]:
        return {"sub": GUEST, "name": "Demo Guest", "acr": "mfa" if client_id == STEPUP_CLIENT else "pwd"}


LOGIN_SCOPES = "openid profile hotel:read booking:read booking:write payment:charge"


class ScriptedLLM:
    """Replays a list of assistant messages: tool calls as (name, args) lists, or final text."""

    def __init__(self, script: Sequence[list[tuple[str, dict[str, Any]]] | str]) -> None:
        self._script = list(script)
        self.seen: list[Sequence[Any]] = []

    async def chat(
        self,
        model: str = "",
        messages: Sequence[Mapping[str, Any] | Message] | None = None,
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> ChatResponse:
        self.seen.append(list(messages or []))
        step = self._script.pop(0)
        if isinstance(step, str):
            return ChatResponse(message=Message(role="assistant", content=step))
        calls = [Message.ToolCall(function=Message.ToolCall.Function(name=name, arguments=args)) for name, args in step]
        return ChatResponse(message=Message(role="assistant", content="", tool_calls=calls))

    def tool_results(self) -> list[str]:
        last = self.seen[-1] if self.seen else []
        return [_content(m) for m in last if _role(m) == "tool"]


def _role(message: Mapping[str, Any] | Message) -> str:
    return message["role"] if isinstance(message, Mapping) else message.role


def _content(message: Mapping[str, Any] | Message) -> str:
    return message["content"] if isinstance(message, Mapping) else message.content or ""


class PortRouter(httpx.AsyncBaseTransport):
    """Sends each request to the in-memory ASGI app registered for its port."""

    def __init__(self, apps: Mapping[int, Any]) -> None:
        self._transports = {port: httpx.ASGITransport(app=app) for port, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transports[request.url.port or 80].handle_async_request(request)


@asynccontextmanager
async def mcp_servers(store: HotelStore, settings: GuardSettings) -> AsyncIterator[PortRouter]:
    """All four guarded servers, running in memory."""
    async with AsyncExitStack() as stack:
        apps = {}
        for module, port in SERVER_MODULES.values():
            server = module.create_server(store)
            apps[port] = guarded_app(server, module.create_rules(store, settings), settings, StaticKeyVerifier())
            await stack.enter_async_context(server.session_manager.run())
        yield PortRouter(apps)


def server_configs() -> list[ServerConfig]:
    return [ServerConfig(name, url=f"http://127.0.0.1:{port}/mcp") for name, (_m, port) in SERVER_MODULES.items()]


def policy() -> StepUpPolicy:
    return load_policy(ROOT / "config" / "stepup_policy.yaml")


def settings(tmp_path: Path, **overrides: str) -> Settings:
    return Settings.from_env(
        {
            "WEB_CLIENT_ID": WEB.client_id,
            "WEB_CLIENT_SECRET": WEB.client_secret,
            "STEPUP_CLIENT_ID": STEPUP.client_id,
            "STEPUP_CLIENT_SECRET": STEPUP.client_secret,
            "AGENT_CLIENT_ID": AGENT.client_id,
            "AGENT_CLIENT_SECRET": AGENT.client_secret,
            "MOCK_PHONE_KEY": "phone-key",
            "THUNDER_BASE": ISSUER,
            "WEB_DIST": str(tmp_path / "no-dist"),
            **overrides,
        }
    )
