"""Shared fixtures: a local RSA key standing in for Thunder, and a JWT factory signed with it."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from servers.guard import Claims, InvalidTokenError
from servers.runner import GuardSettings
from servers.store import HotelStore

ISSUER = "http://idp.test"
RESOURCE = "urn:staywise:hotel-api"
WEB_CLIENT = "staywise-web"
STEPUP_CLIENT = "staywise-stepup"
AGENT_ID = "agent-123"
GUEST = "user-guest"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

TokenFactory = Callable[..., str]


def make_token(
    scopes: str = "",
    *,
    sub: str = GUEST,
    client_id: str = WEB_CLIENT,
    act: dict[str, Any] | None = None,
    aud: str = RESOURCE,
    issuer: str = ISSUER,
    ttl: int = 600,
) -> str:
    """A signed access token shaped like Thunder's."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": issuer,
        "sub": sub,
        "aud": aud,
        "client_id": client_id,
        "scope": scopes,
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    if act:
        payload["act"] = act
    return jwt.encode(payload, _KEY, algorithm="RS256", headers={"kid": "test"})


class StaticKeyVerifier:
    """Verifies tokens from `make_token`, like `JwksTokenVerifier` does with Thunder's JWKS."""

    async def verify(self, token: str) -> Claims:
        try:
            payload = jwt.decode(token, _KEY.public_key(), algorithms=["RS256"], audience=RESOURCE, issuer=ISSUER)
        except jwt.PyJWTError as exc:
            raise InvalidTokenError(str(exc)) from exc
        return Claims.from_payload(payload)


@pytest.fixture
def token() -> TokenFactory:
    return make_token


@pytest.fixture
def store(tmp_path: Path) -> HotelStore:
    return HotelStore(tmp_path / "staywise.db")


@pytest.fixture
def guard_settings() -> GuardSettings:
    return GuardSettings(
        issuer=ISSUER,
        audience=RESOURCE,
        jwks_url=f"{ISSUER}/oauth2/jwks",
        stepup_client_ids=frozenset({STEPUP_CLIENT}),
        high_value_limit=500.0,
    )
