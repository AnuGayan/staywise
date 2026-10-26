"""Bearer-token guard for the StayWise MCP servers (an ASGI middleware in front of FastMCP).

The WSO2 gateway already checks the token and coarse per-tool scopes. This guard repeats the
token check (defense in depth) and adds the rules the gateway cannot express because they
depend on the call's arguments, e.g. "charges over $500 need a fresh MFA token".

Failures use the same RFC 6750 challenges as the gateway, so the agent handles both alike:

    401  WWW-Authenticate: Bearer resource_metadata="…", error="invalid_token"
    403  WWW-Authenticate: Bearer resource_metadata="…", error="insufficient_scope", scope="…"

Verified claims reach the tools through `request.state.staywise_claims` (see `claims_of`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import anyio
import jwt

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

PRM_PATH = "/.well-known/oauth-protected-resource"
CLAIMS_STATE_KEY = "staywise_claims"
MAX_BODY_BYTES = 1_000_000


class InvalidTokenError(Exception):
    """The bearer token is missing, malformed, expired, or not for this resource."""


@dataclass(frozen=True)
class Claims:
    """The parts of a verified access token the servers use."""

    sub: str
    client_id: str
    scopes: frozenset[str]
    act: Mapping[str, Any] | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Claims:
        """Build from a decoded JWT payload. `scope` may be a space-separated string or a list."""
        raw_scope = payload.get("scope", "")
        scopes = raw_scope.split() if isinstance(raw_scope, str) else [str(s) for s in raw_scope]
        act = payload.get("act")
        return cls(
            sub=str(payload.get("sub", "")),
            client_id=str(payload.get("client_id") or payload.get("azp") or ""),
            scopes=frozenset(scopes),
            act=act if isinstance(act, Mapping) else None,
            raw=dict(payload),
        )

    @property
    def acting_agent(self) -> str | None:
        """The immediate actor's `sub` when the token was delegated through token exchange."""
        return str(self.act["sub"]) if self.act and "sub" in self.act else None


class TokenVerifier(Protocol):
    """Turns a bearer token into verified claims."""

    async def verify(self, token: str) -> Claims:
        """Return the token's claims.

        Raises:
            InvalidTokenError: if the token must be rejected.
        """
        ...


class JwksTokenVerifier:
    """Verifies RS/ES-signed JWTs against the issuer's JWKS (keys are cached by PyJWT)."""

    def __init__(self, issuer: str, audience: str, jwks_url: str, leeway_seconds: int = 30) -> None:
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway_seconds
        self._jwks = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300, timeout=5)

    async def verify(self, token: str) -> Claims:
        """Check signature, issuer, audience and expiry.

        Raises:
            InvalidTokenError: if any check fails or the JWKS cannot be fetched.
        """
        try:
            key = await anyio.to_thread.run_sync(self._jwks.get_signing_key_from_jwt, token)
            payload = jwt.decode(
                token,
                key.key,
                algorithms=["RS256", "ES256", "PS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidTokenError(str(exc)) from exc
        return Claims.from_payload(payload)


@dataclass(frozen=True)
class Requirement:
    """What a tool call needs: at least one of `any_scopes`, and optionally a specific client.

    `client_ids` pins the token to apps whose login flow guarantees a property the token
    itself cannot show yet (Thunder 1.0.0 puts `acr` only in ID tokens), e.g. the MFA-only
    step-up app.
    """

    any_scopes: frozenset[str]
    reason: str
    client_ids: frozenset[str] = frozenset()

    def satisfied_by(self, claims: Claims) -> bool:
        """True if the claims meet this requirement."""
        if not self.any_scopes & claims.scopes:
            return False
        return not self.client_ids or claims.client_id in self.client_ids


def requires(*scopes: str, reason: str, client_ids: frozenset[str] = frozenset()) -> Requirement:
    """Shorthand for building a `Requirement`."""
    return Requirement(frozenset(scopes), reason, client_ids)


# A rule sees the tool arguments and returns the requirement for that particular call.
ToolRule = Callable[[Mapping[str, Any]], Requirement]


class AuthGuard:
    """ASGI middleware: authenticates every MCP request and authorizes `tools/call` by rule.

    Tools without a rule are denied, so a newly added tool is closed until someone decides
    who may call it.
    """

    def __init__(
        self,
        app: ASGIApp,
        verifier: TokenVerifier,
        rules: Mapping[str, ToolRule],
        *,
        issuer: str,
        resource_url: str | None = None,
        scopes_supported: list[str] | None = None,
        mcp_path: str = "/mcp",
    ) -> None:
        self._app = app
        self._verifier = verifier
        self._rules = dict(rules)
        self._issuer = issuer
        self._resource_url = resource_url
        self._scopes_supported = scopes_supported or []
        self._mcp_path = mcp_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith(PRM_PATH):
            await self._send_metadata(scope, send)
            return
        if path.rstrip("/") != self._mcp_path:
            await self._app(scope, receive, send)
            return

        body = await _read_body(receive) if scope.get("method") == "POST" else b""
        try:
            claims = await self._verifier.verify(_bearer_token(scope))
        except InvalidTokenError as exc:
            logger.info("rejected token: %s", exc)
            await self._challenge(scope, send, 401, error="invalid_token", description="Access token is invalid")
            return

        requirement = self._requirement_for(body)
        if requirement is not None and not requirement.satisfied_by(claims):
            await self._challenge(
                scope,
                send,
                403,
                error="insufficient_scope",
                description=requirement.reason,
                scopes=sorted(requirement.any_scopes),
            )
            return

        scope.setdefault("state", {})[CLAIMS_STATE_KEY] = claims
        await self._app(scope, _replay(body, receive), send)

    def _requirement_for(self, body: bytes) -> Requirement | None:
        """The requirement for a `tools/call` body; None for every other MCP method."""
        try:
            message = json.loads(body) if body else {}
        except ValueError:
            return None  # let the MCP layer answer with a JSON-RPC parse error
        if not isinstance(message, dict) or message.get("method") != "tools/call":
            return None
        params = message.get("params") or {}
        name = str(params.get("name", ""))
        rule = self._rules.get(name)
        if rule is None:
            return Requirement(frozenset({"__no_such_scope__"}), f"tool {name!r} is not available to any client")
        arguments = params.get("arguments")
        try:
            return rule(arguments if isinstance(arguments, Mapping) else {})
        except (TypeError, ValueError) as exc:
            # Malformed arguments: authorize with the strictest outcome rather than skip the rule.
            return Requirement(frozenset({"__no_such_scope__"}), f"cannot authorize {name}: {exc}")

    def _metadata_url(self, scope: Scope) -> str:
        return f"{self._base_url(scope)}{PRM_PATH}"

    def _base_url(self, scope: Scope) -> str:
        if self._resource_url:
            return self._resource_url.removesuffix(self._mcp_path)
        headers = dict(scope.get("headers") or [])
        host = headers.get(b"host", b"localhost").decode("latin-1")
        return f"{scope.get('scheme', 'http')}://{host}"

    async def _send_metadata(self, scope: Scope, send: Send) -> None:
        document = {
            "resource": f"{self._base_url(scope)}{self._mcp_path}",
            "authorization_servers": [self._issuer],
            "scopes_supported": self._scopes_supported,
            "bearer_methods_supported": ["header"],
        }
        await _send_json(send, 200, document)

    async def _challenge(
        self,
        scope: Scope,
        send: Send,
        status: int,
        *,
        error: str,
        description: str,
        scopes: list[str] | None = None,
    ) -> None:
        parts = [f'resource_metadata="{self._metadata_url(scope)}"', f'error="{error}"']
        if scopes:
            parts.append(f'scope="{" ".join(scopes)}"')
        parts.append(f'error_description="{_quote_safe(description)}"')
        header = "Bearer " + ", ".join(parts)
        await _send_json(
            send, status, {"error": error, "error_description": description}, [(b"www-authenticate", header.encode())]
        )


def claims_of(request: Any) -> Claims:
    """The verified claims the guard attached to a Starlette request.

    Raises:
        PermissionError: if the request did not pass through the guard.
    """
    claims = getattr(getattr(request, "state", None), CLAIMS_STATE_KEY, None)
    if not isinstance(claims, Claims):
        raise PermissionError("request is not authenticated")
    return claims


# The WSO2 gateway's mcp-auth (forwardToken: true) moves the token to X-Forwarded-Authorization.
# Accepting it there grants nothing extra: the token is verified exactly like a direct one.
_TOKEN_HEADERS = (b"authorization", b"x-forwarded-authorization")


def _bearer_token(scope: Scope) -> str:
    found: dict[bytes, str] = {}
    for name, value in scope.get("headers") or []:
        key = name.lower()
        if key in _TOKEN_HEADERS and key not in found:
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                found[key] = token.strip()
    for key in _TOKEN_HEADERS:
        if key in found:
            return found[key]
    raise InvalidTokenError("no bearer token")


def _quote_safe(text: str) -> str:
    return text.replace("\\", "").replace('"', "'")


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            break
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    """A `receive` that yields the already-read body once, then defers to the real one."""
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


async def _send_json(
    send: Send, status: int, document: Mapping[str, Any], extra_headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    payload = json.dumps(document).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers + (extra_headers or [])})
    await send({"type": "http.response.body", "body": payload})
