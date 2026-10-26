"""StayWise concierge MCP server: messages to hotel staff, sent under the agent's own identity.

These tools need `ops:notify`, which only the agent identity holds (client credentials), so
the audit trail shows the agent, not the guest, as the requester.

    python -m servers.concierge_server               # http://127.0.0.1:8104/mcp
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from servers.guard import ToolRule, claims_of, requires
from servers.runner import GuardSettings, run, transport_security
from servers.store import HotelStore

DEFAULT_PORT = 8104
NOTIFY = requires("ops:notify", reason="Contacting hotel staff needs the agent's ops:notify permission")


def create_server(
    store: HotelStore, host: str = "127.0.0.1", port: int = DEFAULT_PORT, allowed_hosts: list[str] | None = None
) -> FastMCP:
    """Build the concierge server with its tools registered."""
    server = FastMCP(
        "staywise-concierge",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
        transport_security=transport_security(port, allowed_hosts or []),
    )

    @server.tool()
    def notify_front_desk(
        message: str, ctx: Context, reservation_id: str | None = None, guest_name: str | None = None
    ) -> dict[str, Any]:
        """Send a note to the hotel front desk, e.g. a late arrival time or a special request."""
        requester = claims_of(ctx.request_context.request).sub
        return store.add_ops_request("front_desk", message, requester, reservation_id, guest_name)

    @server.tool()
    def request_housekeeping(
        request: str, ctx: Context, reservation_id: str | None = None, guest_name: str | None = None
    ) -> dict[str, Any]:
        """Ask housekeeping for something, e.g. extra pillows or a baby cot."""
        requester = claims_of(ctx.request_context.request).sub
        return store.add_ops_request("housekeeping", request, requester, reservation_id, guest_name)

    return server


def create_rules(_store: HotelStore, _settings: GuardSettings) -> Mapping[str, ToolRule]:
    """Both tools need `ops:notify`."""
    return {name: (lambda _args: NOTIFY) for name in ("notify_front_desk", "request_housekeeping")}


def main() -> None:
    """Run the concierge server."""
    run(__doc__ or "", DEFAULT_PORT, create_server, create_rules)


if __name__ == "__main__":
    main()
