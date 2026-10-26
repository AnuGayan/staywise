"""StayWise reservations MCP server: the signed-in guest's bookings.

Reading needs `booking:read`; booking, changing and cancelling need `booking:write`.
Cancelling a paid non-refundable booking forfeits money, so it needs a fresh MFA step-up
(`payment:highvalue` from the step-up app).

    python -m servers.reservations_server            # http://127.0.0.1:8102/mcp
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from servers.guard import Requirement, ToolRule, claims_of, requires
from servers.runner import GuardSettings, run, transport_security
from servers.store import HotelStore, to_dict

DEFAULT_PORT = 8102
READ = requires("booking:read", reason="Viewing reservations needs the booking:read permission")
WRITE = requires("booking:write", reason="Changing reservations needs the booking:write permission")


def create_server(
    store: HotelStore, host: str = "127.0.0.1", port: int = DEFAULT_PORT, allowed_hosts: list[str] | None = None
) -> FastMCP:
    """Build the reservations server with its tools registered."""
    server = FastMCP(
        "staywise-reservations",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
        transport_security=transport_security(port, allowed_hosts or []),
    )

    @server.tool()
    def list_my_reservations(ctx: Context) -> dict[str, Any]:
        """The guest's reservations, newest stay first."""
        guest = claims_of(ctx.request_context.request).sub
        return {"reservations": [to_dict(r) for r in store.list_reservations(guest)]}

    @server.tool()
    def create_reservation(
        room_id: str, check_in: str, check_out: str, ctx: Context, guests: int = 2
    ) -> dict[str, Any]:
        """Book a room for the guest (dates YYYY-MM-DD). Returns the reservation with `deposit_due`;
        take payment separately with charge_payment."""
        claims = claims_of(ctx.request_context.request)
        created_by = claims.acting_agent or claims.client_id
        return to_dict(store.create_reservation(claims.sub, room_id, check_in, check_out, guests, created_by))

    @server.tool()
    def change_room(reservation_id: str, new_room_id: str, ctx: Context) -> dict[str, Any]:
        """Move a reservation to another room type in the same hotel (an upgrade or downgrade). The total is
        repriced; pay any new balance with charge_payment."""
        guest = claims_of(ctx.request_context.request).sub
        return to_dict(store.change_room(guest, reservation_id, new_room_id))

    @server.tool()
    def cancel_reservation(reservation_id: str, ctx: Context) -> dict[str, Any]:
        """Cancel a reservation. Payments on non-refundable rates are not returned."""
        guest = claims_of(ctx.request_context.request).sub
        return to_dict(store.cancel_reservation(guest, reservation_id))

    return server


def create_rules(store: HotelStore, settings: GuardSettings) -> Mapping[str, ToolRule]:
    """Reads need booking:read, writes booking:write; forfeiting a prepaid non-refundable stay needs MFA."""
    high_value = requires(
        "payment:highvalue",
        reason="Cancelling a prepaid non-refundable stay forfeits the payment, so the guest must verify with MFA",
        client_ids=settings.stepup_client_ids,
    )

    def cancel_rule(args: Mapping[str, Any]) -> Requirement:
        reservation = store.find_reservation(str(args.get("reservation_id", "")))
        if reservation is not None and not reservation.refundable and reservation.paid > 0:
            return high_value
        return WRITE

    return {
        "list_my_reservations": lambda _args: READ,
        "create_reservation": lambda _args: WRITE,
        "change_room": lambda _args: WRITE,
        "cancel_reservation": cancel_rule,
    }


def main() -> None:
    """Run the reservations server."""
    run(__doc__ or "", DEFAULT_PORT, create_server, create_rules)


if __name__ == "__main__":
    main()
