"""Parses `WWW-Authenticate: Bearer …` challenges (RFC 6750, and RFC 9470 step-up)."""

from __future__ import annotations

import re
from dataclasses import dataclass

_PARAM = re.compile(r'([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s,]+)')


@dataclass(frozen=True)
class Challenge:
    """What a resource server said is missing."""

    status: int
    error: str | None = None
    scopes: frozenset[str] = frozenset()
    description: str | None = None
    acr_values: str | None = None
    max_age: int | None = None
    resource_metadata: str | None = None

    @property
    def needs_new_token(self) -> bool:
        """401 invalid_token (expired/revoked): the same permissions, freshly issued."""
        return self.status == 401

    @property
    def needs_more(self) -> bool:
        """403 insufficient_scope or 401 insufficient_user_authentication: a stronger token."""
        return self.error in ("insufficient_scope", "insufficient_user_authentication")


def parse_challenge(status: int, header: str | None) -> Challenge:
    """Read the Bearer parameters from a challenge header; missing or foreign headers give a bare Challenge."""
    if not header:
        return Challenge(status)
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return Challenge(status)
    params = {name.lower(): _unquote(value) for name, value in _PARAM.findall(rest)}
    max_age = params.get("max_age")
    return Challenge(
        status=status,
        error=params.get("error"),
        scopes=frozenset(params.get("scope", "").split()),
        description=params.get("error_description"),
        acr_values=params.get("acr_values"),
        max_age=int(max_age) if max_age and max_age.isdigit() else None,
        resource_metadata=params.get("resource_metadata"),
    )


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    return value
