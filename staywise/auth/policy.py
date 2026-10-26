"""Step-up policy: for each scope, how the agent may obtain it. Loaded from YAML, never decided by the LLM.

    auto-obo    mint it by token exchange from the user's login grant (the user consented once at login)
    auto-agent  use the agent's own identity (client credentials)
    user        the user must verify interactively (MFA through the step-up app)

A scope missing from the policy cannot be obtained at all (fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


class Strategy(StrEnum):
    """How a scope is obtained."""

    AUTO_OBO = "auto-obo"
    AUTO_AGENT = "auto-agent"
    USER = "user"


class PolicyError(ValueError):
    """The policy file is missing or malformed."""


# Stronger strategies win when one challenge asks for scopes with different strategies.
_RANK = {Strategy.AUTO_OBO: 0, Strategy.AUTO_AGENT: 1, Strategy.USER: 2}


@dataclass(frozen=True)
class ServerPolicy:
    """Which identity calls a server by default, and the scopes its first token carries."""

    name: str
    identity: Strategy
    baseline: frozenset[str]


@dataclass(frozen=True)
class UserStepUp:
    """How interactive step-up is requested from the IdP."""

    acr_values: str
    scopes: frozenset[str]
    timeout_seconds: float


@dataclass(frozen=True)
class StepUpPolicy:
    """The whole policy."""

    servers: dict[str, ServerPolicy]
    scopes: dict[str, Strategy]
    user_stepup: UserStepUp
    login_scopes: frozenset[str]

    def strategy_for(self, scopes: frozenset[str]) -> Strategy | None:
        """The strategy that can obtain every scope in `scopes`, or None if any is not obtainable."""
        strategies = [self.scopes.get(scope) for scope in scopes]
        if not strategies or None in strategies:
            return None
        known = [strategy for strategy in strategies if strategy is not None]
        if Strategy.AUTO_AGENT in known and Strategy.USER in known:
            return None  # no single token can be both the agent's and the user's
        return max(known, key=_RANK.__getitem__)

    def server(self, name: str) -> ServerPolicy:
        """Policy for a configured server.

        Raises:
            PolicyError: if the server is not in the policy.
        """
        try:
            return self.servers[name]
        except KeyError as exc:
            raise PolicyError(f"server {name!r} is not in the step-up policy") from exc


def load_policy(path: Path) -> StepUpPolicy:
    """Read and validate the YAML policy.

    Raises:
        PolicyError: if the file cannot be read or is invalid.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"cannot read step-up policy {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError(f"{path} must be a mapping")
    try:
        scopes = {str(scope): Strategy(value) for scope, value in _mapping(raw, "scopes").items()}
        servers = {
            name: ServerPolicy(
                name=name,
                identity=Strategy(entry.get("identity", "auto-obo")),
                baseline=frozenset(entry.get("baseline", [])),
            )
            for name, entry in _mapping(raw, "servers").items()
        }
        user = _mapping(raw, "user_stepup")
        user_stepup = UserStepUp(
            acr_values=str(user.get("acr_values", "mfa")),
            scopes=frozenset(user.get("scopes", [])),
            timeout_seconds=float(user.get("timeout_seconds", 120)),
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise PolicyError(f"invalid step-up policy {path}: {exc}") from exc
    for server in servers.values():
        if server.identity == Strategy.USER:
            raise PolicyError(f"server {server.name!r}: identity must be auto-obo or auto-agent")
        unknown = server.baseline - scopes.keys()
        if unknown:
            raise PolicyError(f"server {server.name!r}: baseline scopes not in policy: {sorted(unknown)}")
    return StepUpPolicy(servers, scopes, user_stepup, frozenset(raw.get("login_scopes", [])))


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise PolicyError(f"'{key}' must be a mapping")
    return value
