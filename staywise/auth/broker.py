"""Decides which token each MCP request carries, and how to get a stronger one when a server asks.

The user's login grant (the envelope they consented to) never leaves the agent. Every MCP
server gets a narrower token minted from it:

    baseline           least privilege per server (policy `servers.*.baseline`)
    403 for scope S    S is auto-obo   -> token exchange (subject = user grant, actor = agent): automatic
                       S is auto-agent -> client credentials as the agent: automatic
                       S is user       -> MFA popup through the step-up app: the user decides
    401                the token expired: mint the same scopes again
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from staywise.auth.challenge import Challenge
from staywise.auth.policy import StepUpPolicy, Strategy
from staywise.auth.stepup import StepUpCoordinator, StepUpRefusedError
from staywise.auth.thunder import ClientCredentials, OAuthError, ThunderClient, TokenSet
from staywise.events import CallContext

EXPIRY_MARGIN_SECONDS = 20
USER_MFA = "user-mfa"


@dataclass(frozen=True)
class IssuedToken:
    """A token the agent may send, and how it was obtained (shown in the UI's security timeline)."""

    value: str
    scopes: frozenset[str]
    expires_at: float
    strategy: str
    minted_at: float
    claims: Mapping[str, Any] = field(default_factory=dict)

    def usable(self, now: float) -> bool:
        """Not expired, with a margin for clock skew and request time."""
        return self.expires_at - EXPIRY_MARGIN_SECONDS > now

    @classmethod
    def from_token_set(cls, tokens: TokenSet, strategy: str, now: float) -> IssuedToken:
        """Wrap a token endpoint response."""
        return cls(tokens.access_token, tokens.scopes, tokens.expires_at, strategy, now, tokens.claims)


@dataclass
class UserGrant:
    """The signed-in user's login tokens (refreshed in place)."""

    sub: str
    tokens: TokenSet


class TokenBroker:
    """One per chat session."""

    def __init__(
        self,
        thunder: ThunderClient,
        policy: StepUpPolicy,
        *,
        web_client: ClientCredentials,
        agent_client: ClientCredentials,
        grant: UserGrant,
        stepups: StepUpCoordinator,
        calls: CallContext,
        actor_scopes: frozenset[str] = frozenset(),
        use_actor_token: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._thunder = thunder
        self._policy = policy
        self._web = web_client
        self._agent = agent_client
        self.grant = grant
        self._stepups = stepups
        self._calls = calls
        self._actor_scopes = actor_scopes
        self._use_actor = use_actor_token
        self._clock = clock
        self._server_tokens: dict[str, IssuedToken] = {}
        self._actor: IssuedToken | None = None
        self._mfa: IssuedToken | None = None

    async def token_for(self, server: str) -> IssuedToken:
        """The token to send to `server` first: its cached token, or a freshly minted baseline one.

        Raises:
            StepUpRefusedError: if no token can be obtained.
        """
        now = self._clock()
        cached = self._server_tokens.get(server)
        if cached and cached.usable(now):
            return cached
        server_policy = self._policy.server(server)
        # On expiry keep any scopes the server was already stepped up to, so the user isn't asked twice.
        scopes = server_policy.baseline | (cached.scopes if cached else frozenset())
        token = await self._mint(server_policy.identity, scopes & self._policy.scopes.keys())
        self._server_tokens[server] = token
        return token

    async def resolve(self, server: str, current: IssuedToken | None, challenge: Challenge) -> IssuedToken:
        """A token that should satisfy `challenge`.

        Raises:
            StepUpRefusedError: if policy forbids it, the IdP refuses, or the user declines.
        """
        if challenge.needs_new_token and not challenge.needs_more:
            self._forget(server, current)
            return await self.token_for(server)
        wanted = challenge.scopes
        if not challenge.needs_more or not wanted:
            raise StepUpRefusedError(f"the server refused the request: {challenge.description or challenge.error}")
        strategy = self._policy.strategy_for(wanted)
        if strategy is None:
            raise StepUpRefusedError(f"this needs {' '.join(sorted(wanted))}, which the agent is not allowed to obtain")
        # Holding the scope yet being refused means minting the same kind of token again cannot help.
        if (
            current is not None
            and wanted <= current.scopes
            and (strategy != Strategy.USER or current.strategy == USER_MFA)
        ):
            raise StepUpRefusedError(f"the server still refused the request: {challenge.description}")
        if strategy == Strategy.USER:
            return await self._user_token(wanted, challenge.description or "This action needs extra verification")

        server_policy = self._policy.server(server)
        base = current.scopes if current and current.strategy == strategy.value else frozenset()
        if not base and server_policy.identity == strategy:
            base = server_policy.baseline
        token = await self._mint(strategy, (base | wanted) & self._policy.scopes.keys())
        if not wanted <= token.scopes:
            missing = " ".join(sorted(wanted - token.scopes))
            raise StepUpRefusedError(f"the identity provider did not grant {missing}")
        if server_policy.identity == strategy:
            self._server_tokens[server] = token
        return token

    def granted_scopes(self) -> frozenset[str]:
        """What the user consented to at login."""
        return self.grant.tokens.scopes

    async def _mint(self, strategy: Strategy, scopes: frozenset[str]) -> IssuedToken:
        try:
            if strategy == Strategy.AUTO_AGENT:
                tokens = await self._thunder.client_credentials(self._agent, scopes)
            else:
                actor = await self._actor_token() if self._use_actor else None
                tokens = await self._thunder.token_exchange(self._web, await self._user_access(), scopes, actor)
        except OAuthError as exc:
            raise StepUpRefusedError(f"the identity provider refused the token request ({exc.error})") from exc
        return IssuedToken.from_token_set(tokens, strategy.value, self._clock())

    async def _user_token(self, wanted: frozenset[str], reason: str) -> IssuedToken:
        if self._mfa and self._mfa.usable(self._clock()) and wanted <= self._mfa.scopes:
            return self._mfa
        requestable = self._policy.user_stepup.scopes
        if not wanted <= requestable:
            raise StepUpRefusedError(f"interactive verification cannot grant {' '.join(sorted(wanted - requestable))}")
        tokens = await self._stepups.request(wanted, reason, self._calls.current)
        token = IssuedToken.from_token_set(tokens, USER_MFA, self._clock())
        if not wanted <= token.scopes:
            raise StepUpRefusedError("the verification finished but did not grant the needed permission")
        self._mfa = token
        return token

    async def _user_access(self) -> str:
        tokens = self.grant.tokens
        if tokens.expires_at - EXPIRY_MARGIN_SECONDS > self._clock():
            return tokens.access_token
        if not tokens.refresh_token:
            raise StepUpRefusedError("the guest's sign-in has expired; they need to sign in again")
        try:
            refreshed = await self._thunder.refresh(self._web, tokens.refresh_token)
        except OAuthError as exc:
            raise StepUpRefusedError("the guest's sign-in has expired; they need to sign in again") from exc
        # Thunder rotates refresh tokens and revokes the whole family if an old one is replayed,
        # so always keep the newest (and the old one only if none came back).
        if not refreshed.refresh_token:
            refreshed = replace(refreshed, refresh_token=tokens.refresh_token)
        self.grant.tokens = refreshed
        return refreshed.access_token

    async def _actor_token(self) -> str:
        if self._actor is None or not self._actor.usable(self._clock()):
            tokens = await self._thunder.client_credentials(self._agent, self._actor_scopes)
            self._actor = IssuedToken.from_token_set(tokens, Strategy.AUTO_AGENT.value, self._clock())
        return self._actor.value

    def _forget(self, server: str, token: IssuedToken | None) -> None:
        if token is not None and token.strategy == USER_MFA:
            self._mfa = None
        else:
            self._server_tokens.pop(server, None)
