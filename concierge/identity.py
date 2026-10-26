"""Who the agent is serving, read from the user token the UI forwards.

The Agent Manager gateway validates that token before a request reaches the agent, so the agent only
decodes it (no signature, expiry or audience checks here) to learn the guest's username, then maps the
username to a Unity by Hard Rock member id.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt

FORWARDED_AUTHORIZATION = "x-forwarded-authorization"


class MemberMapError(ValueError):
    """The username -> member id map is missing or malformed."""


class IdentityError(Exception):
    """The forwarded token doesn't identify a Unity member. `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Guest:
    """The signed-in guest a request is for."""

    subject: str
    username: str
    member_id: str


def load_members(path: Path) -> dict[str, str]:
    """Read the ThunderID username -> Unity member id map.

    Raises:
        MemberMapError: if the file is unreadable or not a flat string-to-string object.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemberMapError(f"cannot read member map {path}: {exc}") from exc
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
        raise MemberMapError(f"{path} must be a JSON object mapping usernames to Unity member ids")
    return {username.lower(): member_id for username, member_id in raw.items()}


def bearer_token(headers: Mapping[str, str]) -> str | None:
    """The user token from `Authorization`, or from `X-Forwarded-Authorization` where the gateway's JWT
    policy forwards it after validation (it removes `Authorization`, and may strip the `Bearer` scheme).

    Accepts `Bearer <jwt>` or a bare JWT in either header.
    """
    for name in ("authorization", FORWARDED_AUTHORIZATION):
        value = headers.get(name, "").strip()
        scheme, _, rest = value.partition(" ")
        if scheme.lower() == "bearer" and rest.strip():
            return rest.strip()
        if _looks_like_jwt(value):
            return value
    return None


def _looks_like_jwt(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 3 and all(parts[:2]) and " " not in value


def unverified_claims(token: str) -> dict[str, Any]:
    """Decode a JWT's claims WITHOUT checking it. Only for tokens something else already validated.

    Raises:
        jwt.DecodeError: if the token is not a JWT.
    """
    return jwt.decode(token, options={"verify_signature": False})


def guest_from_token(token: str, members: Mapping[str, str]) -> Guest:
    """Identify the guest from a gateway-validated user token.

    Raises:
        IdentityError: 401 if the token isn't a JWT or names no user, 403 if the user has no membership.
    """
    try:
        claims = unverified_claims(token)
    except jwt.DecodeError as exc:
        raise IdentityError(f"the forwarded token is not a JWT: {exc}", 401) from exc
    subject = claims.get("sub")
    username = str(claims.get("username") or claims.get("preferred_username") or "")
    if not subject or not username:
        raise IdentityError("the forwarded token has no sub/username claim; is it a user token?", 401)
    member_id = members.get(username.lower())
    if member_id is None:
        raise IdentityError(f"the account '{username}' is not linked to a Unity by Hard Rock membership", 403)
    return Guest(subject=str(subject), username=username, member_id=member_id)
