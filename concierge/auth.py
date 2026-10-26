"""Browser sign-in against the Agent Manager ThunderID: OIDC authorization code + PKCE, run server-side.

The browser only ever holds an opaque, HttpOnly session cookie. Codes, tokens and the client secret
stay on the server. The signed-in guest's Unity member id comes from their verified ID token
(username -> member id), never from the browser.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

DEFAULT_ISSUER = "http://default-idp.amp.localhost:8080"
DEFAULT_CLIENT_ID = "hardrock-concierge-web"
DEFAULT_RESOURCE = "urn:hardrock:concierge-api"
DEFAULT_PUBLIC_URL = "http://localhost:9100"
DEFAULT_SCOPES = ("openid", "profile", "hotel:read", "bookings:read", "bookings:write", "unity:read")
ID_TOKEN_ALGORITHMS = ["RS256", "ES256"]
CLOCK_SKEW_SECONDS = 30
LOGIN_TTL_SECONDS = 600
DEFAULT_SESSION_TTL_SECONDS = 3600
MAX_PENDING_LOGINS = 1000
MAX_SESSIONS = 1000


class AuthConfigError(ValueError):
    """Sign-in is not configured: a required setting is missing or the member map is unreadable."""


class AuthError(Exception):
    """A sign-in attempt failed. The message is safe to show to the guest."""


@dataclass(frozen=True)
class OidcSettings:
    """Where ThunderID is and how the concierge is registered there."""

    issuer: str
    client_id: str
    client_secret: str
    public_url: str
    resource: str
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    # Where this process reaches the same ThunderID for back-channel calls (discovery, token, JWKS) when
    # the browser-facing issuer URL doesn't resolve from here, e.g. inside an Agent Manager pod.
    internal_url: str | None = None

    @property
    def base_path(self) -> str:
        """The path prefix the concierge is served under, e.g. `/melody` behind the Agent Manager gateway."""
        return urlsplit(self.public_url).path.rstrip("/")

    def backchannel(self, url: str) -> str:
        """Rewrite an issuer-hosted endpoint URL onto `internal_url`, when one is set."""
        if self.internal_url and url.startswith(self.issuer):
            return self.internal_url + url[len(self.issuer) :]
        return url

    @property
    def redirect_uri(self) -> str:
        """The callback registered on the Thunder app for this public URL."""
        return f"{self.public_url}/auth/callback"

    @property
    def secure_cookies(self) -> bool:
        """Mark the session cookie Secure when the concierge is served over HTTPS."""
        return self.public_url.startswith("https://")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> OidcSettings:
        """Read settings from the variables thunder-setup.sh writes to .env.thunder.

        Raises:
            AuthConfigError: if CONCIERGE_CLIENT_SECRET is not set.
        """
        secret = environ.get("CONCIERGE_CLIENT_SECRET", "")
        if not secret:
            raise AuthConfigError(
                "CONCIERGE_CLIENT_SECRET is not set; run wso2/thunder/thunder-setup.sh --from-cluster, "
                "then `set -a; source .env.thunder; set +a`"
            )
        return cls(
            issuer=environ.get("THUNDER_ISSUER", DEFAULT_ISSUER).rstrip("/"),
            client_id=environ.get("CONCIERGE_CLIENT_ID", DEFAULT_CLIENT_ID),
            client_secret=secret,
            public_url=environ.get("CONCIERGE_PUBLIC_URL", DEFAULT_PUBLIC_URL).rstrip("/"),
            resource=environ.get("THUNDER_RESOURCE", DEFAULT_RESOURCE),
            internal_url=environ.get("THUNDER_INTERNAL_URL", "").rstrip("/") or None,
        )


def load_members(path: Path) -> dict[str, str]:
    """Read the Thunder username -> Unity member id map.

    Raises:
        AuthConfigError: if the file is unreadable or not a flat string-to-string object.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthConfigError(f"cannot read member map {path}: {exc}") from exc
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
        raise AuthConfigError(f"{path} must be a JSON object mapping usernames to Unity member ids")
    return {username.lower(): member_id for username, member_id in raw.items()}


@dataclass(frozen=True)
class SignedInGuest:
    """Who is using the chat, as established by their ID token."""

    subject: str
    username: str
    name: str
    member_id: str


@dataclass(frozen=True)
class TokenGrant:
    """The parts of a token response the concierge uses."""

    id_token: str
    expires_in: int


class IdentityProvider(Protocol):
    """The OIDC operations `WebAuth` needs (see `OidcClient`)."""

    async def authorization_url(self, state: str, nonce: str, code_challenge: str) -> str:
        """Where to send the browser to sign in."""
        ...

    async def exchange_code(self, code: str, code_verifier: str) -> TokenGrant:
        """Redeem an authorization code."""
        ...

    async def verify_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        """Check an ID token's signature and claims; return the claims."""
        ...


class OidcClient:
    """ThunderID's discovery, token and JWKS endpoints, for one confidential client."""

    def __init__(self, settings: OidcSettings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._metadata: dict[str, Any] | None = None
        self._jwks: jwt.PyJWKSet | None = None

    async def authorization_url(self, state: str, nonce: str, code_challenge: str) -> str:
        """The authorize URL. `prompt=login` makes every sign-in ask for credentials, so guests can switch."""
        query = {
            "response_type": "code",
            "client_id": self._settings.client_id,
            "redirect_uri": self._settings.redirect_uri,
            "scope": " ".join(self._settings.scopes),
            "resource": self._settings.resource,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
        }
        endpoint = (await self._discover())["authorization_endpoint"]
        return f"{endpoint}?{urlencode(query)}"

    async def exchange_code(self, code: str, code_verifier: str) -> TokenGrant:
        """Redeem the code at the token endpoint with client_secret_basic.

        Raises:
            AuthError: if the IdP rejects the code or returns no ID token.
        """
        response = await self._http.post(
            self._settings.backchannel((await self._discover())["token_endpoint"]),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.redirect_uri,
                "code_verifier": code_verifier,
                "resource": self._settings.resource,
            },
            auth=(self._settings.client_id, self._settings.client_secret),
        )
        body = _json_object(response)
        if response.status_code != httpx.codes.OK:
            raise AuthError(f"ThunderID rejected the sign-in ({body.get('error', response.status_code)})")
        id_token = body.get("id_token")
        if not isinstance(id_token, str):
            raise AuthError("ThunderID returned no ID token; is 'openid' an allowed scope?")
        return TokenGrant(id_token=id_token, expires_in=int(body.get("expires_in", DEFAULT_SESSION_TTL_SECONDS)))

    async def verify_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        """Verify signature (JWKS, refetched once on an unknown key), issuer, audience, expiry and nonce.

        Raises:
            AuthError: if the token fails any check.
        """
        try:
            header = jwt.get_unverified_header(id_token)
            key = await self._signing_key(header.get("kid"), header.get("alg"))
            claims = jwt.decode(
                id_token,
                key=key.key,
                algorithms=ID_TOKEN_ALGORITHMS,
                audience=self._settings.client_id,
                issuer=self._settings.issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"the ID token from ThunderID is not valid: {exc}") from exc
        if claims.get("nonce") != nonce:
            raise AuthError("the ID token does not belong to this sign-in (nonce mismatch)")
        return claims

    async def _signing_key(self, kid: str | None, alg: str | None) -> jwt.PyJWK:
        """The JWKS key named by `kid` (or, without a kid, the one for `alg`)."""
        for refresh in (False, True):
            keys = await self._key_set(refresh)
            for key in keys.keys:
                if (key.key_id == kid) if kid else (key.algorithm_name == alg):
                    return key
        raise jwt.InvalidKeyError(f"no ThunderID signing key with kid {kid!r} / alg {alg!r}")

    async def _key_set(self, refresh: bool) -> jwt.PyJWKSet:
        if self._jwks is None or refresh:
            response = await self._http.get(self._settings.backchannel((await self._discover())["jwks_uri"]))
            response.raise_for_status()
            self._jwks = jwt.PyJWKSet.from_dict(response.json())
        return self._jwks

    async def _discover(self) -> dict[str, Any]:
        if self._metadata is None:
            base = self._settings.internal_url or self._settings.issuer
            response = await self._http.get(f"{base}/.well-known/openid-configuration")
            response.raise_for_status()
            self._metadata = response.json()
        return self._metadata


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


@dataclass(frozen=True)
class _PendingLogin:
    code_verifier: str
    nonce: str
    started_at: float


@dataclass(frozen=True)
class _Session:
    guest: SignedInGuest
    expires_at: float


class WebAuth:
    """Sign-in state for the web chat: pending logins keyed by `state`, and sessions keyed by cookie value."""

    def __init__(
        self,
        idp: IdentityProvider,
        members: Mapping[str, str],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._idp = idp
        self._members = members
        self._clock = clock
        self._pending: OrderedDict[str, _PendingLogin] = OrderedDict()
        self._sessions: OrderedDict[str, _Session] = OrderedDict()

    async def begin_login(self) -> str:
        """Start a sign-in and return the URL to redirect the browser to."""
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        self._pending[state] = _PendingLogin(verifier, nonce, self._clock())
        while len(self._pending) > MAX_PENDING_LOGINS:
            self._pending.popitem(last=False)
        return await self._idp.authorization_url(state, nonce, _code_challenge(verifier))

    async def complete_login(self, state: str, code: str) -> tuple[str, int]:
        """Finish a sign-in from the callback. Returns the new session id and its lifetime in seconds.

        Raises:
            AuthError: if the state is unknown or stale, the token is invalid, or the user has no membership.
        """
        pending = self._pending.pop(state, None)
        if pending is None or self._clock() - pending.started_at > LOGIN_TTL_SECONDS:
            raise AuthError("this sign-in link has expired or was already used; please sign in again")
        grant = await self._idp.exchange_code(code, pending.code_verifier)
        claims = await self._idp.verify_id_token(grant.id_token, pending.nonce)
        guest = self._guest_from(claims)
        ttl = max(60, min(grant.expires_in, DEFAULT_SESSION_TTL_SECONDS))
        session_id = secrets.token_urlsafe(32)
        self._sessions[session_id] = _Session(guest, self._clock() + ttl)
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.popitem(last=False)
        return session_id, ttl

    def guest(self, session_id: str | None) -> SignedInGuest | None:
        """The signed-in guest for a session cookie, or None if absent or expired."""
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if self._clock() >= session.expires_at:
            del self._sessions[session_id]
            return None
        return session.guest

    def logout(self, session_id: str | None) -> None:
        """Forget a session (a no-op for unknown ids)."""
        if session_id:
            self._sessions.pop(session_id, None)

    def _guest_from(self, claims: Mapping[str, Any]) -> SignedInGuest:
        username = str(claims.get("username") or claims.get("preferred_username") or "")
        if not username:
            raise AuthError("the ID token has no username claim; add 'username' to the app's ID token attributes")
        member_id = self._members.get(username.lower())
        if member_id is None:
            raise AuthError(f"the account '{username}' is not linked to a Unity by Hard Rock membership")
        return SignedInGuest(
            subject=str(claims["sub"]),
            username=username,
            name=str(claims.get("name") or username),
            member_id=member_id,
        )


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
