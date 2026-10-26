"""Shared start-up for the StayWise MCP servers: settings, the auth guard, and the HTTP server."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from servers.guard import ASGIApp, AuthGuard, JwksTokenVerifier, TokenVerifier, ToolRule
from servers.store import DEFAULT_DB_PATH, HotelStore
from staywise.envfile import load_env_files

DEFAULT_ISSUER = "http://default-idp.amp.localhost:8080"
DEFAULT_RESOURCE = "urn:staywise:hotel-api"
DEFAULT_HIGH_VALUE_LIMIT = 500.0
UNCONFIGURED_CLIENT = "<STEPUP_CLIENT_ID not configured>"
ALL_SCOPES = ["hotel:read", "booking:read", "booking:write", "payment:charge", "payment:highvalue", "ops:notify"]


@dataclass(frozen=True)
class GuardSettings:
    """Token validation settings shared by every server."""

    issuer: str
    audience: str
    jwks_url: str
    stepup_client_ids: frozenset[str]
    high_value_limit: float

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GuardSettings:
        """Read THUNDER_BASE, THUNDER_RESOURCE, THUNDER_JWKS_URL, STEPUP_CLIENT_ID, HIGH_VALUE_LIMIT."""
        values = os.environ if env is None else env
        issuer = values.get("THUNDER_BASE", DEFAULT_ISSUER).rstrip("/")
        stepup = {client.strip() for client in values.get("STEPUP_CLIENT_ID", "").split(",") if client.strip()}
        # Thunder 1.0.0 does not limit an app to its configured scopes, so the client binding is what
        # proves MFA happened: without a configured step-up client, high-value calls are refused.
        stepup = stepup or {UNCONFIGURED_CLIENT}
        return cls(
            issuer=issuer,
            audience=values.get("THUNDER_RESOURCE", DEFAULT_RESOURCE),
            jwks_url=values.get("THUNDER_JWKS_URL", f"{issuer}/oauth2/jwks"),
            stepup_client_ids=frozenset(stepup),
            high_value_limit=float(values.get("HIGH_VALUE_LIMIT", DEFAULT_HIGH_VALUE_LIMIT)),
        )


ServerFactory = Callable[[HotelStore, str, int, list[str]], FastMCP]
RulesFactory = Callable[[HotelStore, GuardSettings], Mapping[str, ToolRule]]


def transport_security(port: int, allowed_hosts: list[str]) -> TransportSecuritySettings | None:
    """Keep DNS-rebinding protection on, but accept a gateway's Host header with or without port."""
    if not allowed_hosts:
        return None
    extra = [host for name in allowed_hosts for host in (name, f"{name}:{port}")]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *extra]
    )


def guarded_app(
    server: FastMCP,
    rules: Mapping[str, ToolRule],
    settings: GuardSettings,
    verifier: TokenVerifier | None = None,
    public_url: str | None = None,
) -> ASGIApp:
    """FastMCP's Streamable HTTP app wrapped in the auth guard."""
    return AuthGuard(
        server.streamable_http_app(),
        verifier or JwksTokenVerifier(settings.issuer, settings.audience, settings.jwks_url),
        rules,
        issuer=settings.issuer,
        resource_url=public_url,
        scopes_supported=ALL_SCOPES,
    )


def run(description: str, default_port: int, create_server: ServerFactory, create_rules: RulesFactory) -> None:
    """Parse the common CLI flags and serve one guarded MCP server."""
    load_env_files()
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="extra Host header to accept, e.g. 192.168.5.2 for the gateway in Agent Manager (repeatable)",
    )
    parser.add_argument(
        "--public-url", help="this server's MCP URL as clients see it (for protected resource metadata)"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    settings = GuardSettings.from_env()
    store = HotelStore(args.db)
    server = create_server(store, args.host, args.port, args.allowed_host)
    app = guarded_app(server, create_rules(store, settings), settings, public_url=args.public_url)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
