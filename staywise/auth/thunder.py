"""OAuth 2.0 / OIDC client for ThunderID: login, refresh, client credentials, token exchange."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import anyio
import httpx
import jwt

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


class OAuthError(Exception):
    """The authorization server refused a request."""

    def __init__(self, error: str, description: str = "", status: int = 400) -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error
        self.description = description
        self.status = status


@dataclass(frozen=True)
class ClientCredentials:
    """A confidential client registered in Thunder."""

    client_id: str
    client_secret: str


@dataclass(frozen=True)
class TokenSet:
    """A token endpoint response, with the access token's claims decoded for display and checks."""

    access_token: str
    expires_at: float
    scopes: frozenset[str]
    refresh_token: str | None = None
    id_token: str | None = None
    claims: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Pkce:
    """An RFC 7636 verifier and its S256 challenge."""

    verifier: str
    challenge: str

    @classmethod
    def new(cls) -> Pkce:
        """A fresh random verifier."""
        verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return cls(verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii"))


def peek_claims(token: str) -> dict[str, Any]:
    """Decode a JWT's payload WITHOUT verifying it. Only for tokens received straight from the token
    endpoint, to read their scope/expiry and show them; never to make a trust decision on input."""
    try:
        payload = jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
    except jwt.PyJWTError:
        return {}
    return payload if isinstance(payload, dict) else {}


class ThunderClient:
    """Talks to one Thunder issuer. Every token request is bound to `resource` (RFC 8707).

    `base_url` is the issuer as the browser sees it (authorize redirects, the `iss` check).
    `internal_url`, when set, is used instead for server-to-server calls (token, JWKS).
    """

    def __init__(
        self,
        base_url: str,
        resource: str,
        *,
        internal_url: str | None = None,
        http: httpx.AsyncClient | None = None,
        clock: Any = time.time,
    ) -> None:
        self.issuer = base_url.rstrip("/")
        self.resource = resource
        back_channel = (internal_url or self.issuer).rstrip("/")
        self.authorize_endpoint = f"{self.issuer}/oauth2/authorize"
        self.token_endpoint = f"{back_channel}/oauth2/token"
        self.jwks_uri = f"{back_channel}/oauth2/jwks"
        self._http = http or httpx.AsyncClient(timeout=15)
        self._clock = clock
        self._jwks = jwt.PyJWKClient(self.jwks_uri, cache_keys=True, lifespan=300, timeout=5)

    async def aclose(self) -> None:
        """Release the HTTP connection pool."""
        await self._http.aclose()

    def authorization_url(
        self,
        client_id: str,
        redirect_uri: str,
        scope: str,
        *,
        state: str,
        nonce: str,
        pkce: Pkce,
        acr_values: str | None = None,
        prompt: str | None = None,
        max_age: int | None = None,
    ) -> str:
        """The URL to send the browser to for an authorization code (PKCE S256)."""
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce.challenge,
            "code_challenge_method": "S256",
            "resource": self.resource,
        }
        if acr_values:
            params["acr_values"] = acr_values
        if prompt:
            params["prompt"] = prompt
        if max_age is not None:
            params["max_age"] = str(max_age)
        return f"{self.authorize_endpoint}?{urlencode(params)}"

    async def exchange_code(self, client: ClientCredentials, code: str, redirect_uri: str, verifier: str) -> TokenSet:
        """Redeem an authorization code."""
        return await self._token(
            client,
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier},
        )

    async def refresh(self, client: ClientCredentials, refresh_token: str) -> TokenSet:
        """Use a refresh token. Thunder rotates it, so callers must keep the returned one."""
        return await self._token(client, {"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def client_credentials(self, client: ClientCredentials, scopes: frozenset[str]) -> TokenSet:
        """A token for the client itself (here: the agent's own identity)."""
        form = {"grant_type": "client_credentials"}
        if scopes:
            form["scope"] = " ".join(sorted(scopes))
        return await self._token(client, form)

    async def token_exchange(
        self,
        client: ClientCredentials,
        subject_token: str,
        scopes: frozenset[str],
        actor_token: str | None = None,
    ) -> TokenSet:
        """RFC 8693 exchange. Thunder only narrows: scopes not in the subject token are dropped."""
        form = {
            "grant_type": TOKEN_EXCHANGE_GRANT,
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "requested_token_type": ACCESS_TOKEN_TYPE,
            "scope": " ".join(sorted(scopes)),
        }
        if actor_token:
            form["actor_token"] = actor_token
            form["actor_token_type"] = ACCESS_TOKEN_TYPE
        return await self._token(client, form)

    async def verify_id_token(self, id_token: str, client_id: str, nonce: str) -> dict[str, Any]:
        """Check an ID token's signature, issuer, audience, expiry and nonce.

        Raises:
            OAuthError: if any check fails.
        """
        try:
            key = await anyio.to_thread.run_sync(self._jwks.get_signing_key_from_jwt, id_token)
            claims = jwt.decode(
                id_token, key.key, algorithms=["RS256", "ES256"], audience=client_id, issuer=self.issuer, leeway=30
            )
        except jwt.PyJWTError as exc:
            raise OAuthError("invalid_id_token", str(exc)) from exc
        if claims.get("nonce") != nonce:
            raise OAuthError("invalid_id_token", "nonce mismatch")
        return claims

    async def _token(self, client: ClientCredentials, form: dict[str, str]) -> TokenSet:
        form = {**form, "resource": self.resource}
        try:
            response = await self._http.post(
                self.token_endpoint, data=form, auth=(client.client_id, client.client_secret)
            )
        except httpx.HTTPError as exc:
            raise OAuthError("temporarily_unavailable", f"token endpoint unreachable: {exc}", 503) from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.is_error or "access_token" not in body:
            raise OAuthError(
                str(body.get("error", "server_error")),
                str(body.get("error_description", response.text[:200])),
                response.status_code,
            )
        access = str(body["access_token"])
        claims = peek_claims(access)
        # Trust what the token itself carries: when Thunder drops every requested scope in an exchange it
        # still answers 200, with no `scope` claim at all.
        scope = claims.get("scope", "") if claims else body.get("scope", "")
        expires_in = float(body.get("expires_in") or 300)
        return TokenSet(
            access_token=access,
            expires_at=float(claims.get("exp") or self._clock() + expires_in),
            scopes=frozenset(str(scope).split()),
            refresh_token=body.get("refresh_token"),
            id_token=body.get("id_token"),
            claims=claims,
        )
