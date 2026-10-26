"""Pieces every Hard Rock MCP server shares: FastMCP setup, CLI flags and argument parsing."""

from __future__ import annotations

import argparse
from datetime import date

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


def new_server(
    name: str,
    instructions: str,
    host: str,
    port: int,
    allowed_hosts: list[str] | None = None,
) -> FastMCP:
    """Build a FastMCP server tuned to sit behind a WSO2 gateway MCP proxy.

    Stateless Streamable HTTP with plain JSON replies, so any gateway replica can forward any request.
    """
    return FastMCP(
        name,
        instructions=instructions,
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
        transport_security=transport_security(port, allowed_hosts),
    )


def transport_security(port: int, allowed_hosts: list[str] | None) -> TransportSecuritySettings | None:
    """Keep DNS-rebinding protection on, but let a gateway reach us under extra Host names."""
    if not allowed_hosts:
        return None  # FastMCP's default: protection on for loopback binds only.
    # Proxies that rewrite Host (e.g. WSO2 gateway `hostRewrite: auto`) may omit the port.
    extra = [host for name in allowed_hosts for host in (name, f"{name}:{port}")]
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", *extra]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts)


def base_parser(description: str | None, default_port: int) -> argparse.ArgumentParser:
    """CLI flags shared by every server: transport, bind address, port and extra allowed Host names."""
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=["streamable-http", "stdio"], default="streamable-http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="extra Host header name to accept, e.g. 192.168.5.2 for a gateway in k3d (repeatable)",
    )
    return parser


def parse_date(value: str, field: str) -> date:
    """Parse an ISO date sent by the model, with an error message it can act on.

    Raises:
        ValueError: if `value` is not a YYYY-MM-DD date.
    """
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"{field} must be a date in YYYY-MM-DD format, e.g. 2026-12-12; got {value!r}") from None


def require_member(member_id: str, known: set[str]) -> str:
    """Normalise a Unity member id and check it exists.

    Raises:
        ValueError: if the id is not a known Unity member.
    """
    clean = member_id.strip().upper()
    if clean not in known:
        raise ValueError(f"unknown Unity member id {member_id!r}; ids look like HR-100245")
    return clean
