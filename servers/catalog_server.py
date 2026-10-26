"""StayWise catalog MCP server: hotel search, room details and availability (needs `hotel:read`).

python -m servers.catalog_server                 # http://127.0.0.1:8101/mcp
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp.server.fastmcp import FastMCP

from servers.guard import ToolRule, requires
from servers.runner import GuardSettings, run, transport_security
from servers.store import DEPOSIT_RATE, HotelStore, parse_dates, to_dict

DEFAULT_PORT = 8101
READ = requires("hotel:read", reason="Browsing hotels needs the hotel:read permission")


def create_server(
    store: HotelStore, host: str = "127.0.0.1", port: int = DEFAULT_PORT, allowed_hosts: list[str] | None = None
) -> FastMCP:
    """Build the catalog server with its tools registered."""
    server = FastMCP(
        "staywise-catalog",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
        transport_security=transport_security(port, allowed_hosts or []),
    )

    @server.tool()
    def search_hotels(
        city: str | None = None,
        max_nightly_rate: float | None = None,
        sea_view: bool | None = None,
        check_in: str | None = None,
        check_out: str | None = None,
    ) -> dict[str, Any]:
        """Find room types. Filters are optional: city (e.g. 'Galle'), max nightly rate in USD, sea view.
        Pass check_in and check_out (YYYY-MM-DD) to only list rooms still available for those nights."""
        rooms = store.search_rooms(city, max_nightly_rate, sea_view)
        if check_in and check_out:
            rooms = [room for room in rooms if store.rooms_left(room.id, check_in, check_out) > 0]
        return {"rooms": [to_dict(room) for room in rooms]}

    @server.tool()
    def get_room(room_id: str) -> dict[str, Any]:
        """Details of one room type by its id (e.g. 'lighthouse-deluxe')."""
        return to_dict(store.get_room(room_id))

    @server.tool()
    def check_availability(room_id: str, check_in: str, check_out: str) -> dict[str, Any]:
        """Rooms left and the full price for a stay (dates YYYY-MM-DD). Non-refundable rates are prepaid in full;
        refundable rates take a 25% deposit."""
        room = store.get_room(room_id)
        _, _, nights = parse_dates(check_in, check_out)
        total = room.nightly_rate * nights
        return {
            "room_id": room.id,
            "room_name": room.name,
            "hotel_name": room.hotel_name,
            "nights": nights,
            "rooms_left": store.rooms_left(room_id, check_in, check_out),
            "total": total,
            "refundable": room.refundable,
            "due_at_booking": round(total * DEPOSIT_RATE, 2) if room.refundable else total,
        }

    return server


def create_rules(_store: HotelStore, _settings: GuardSettings) -> Mapping[str, ToolRule]:
    """Every catalog tool needs `hotel:read`."""
    return {name: (lambda _args: READ) for name in ("search_hotels", "get_room", "check_availability")}


def main() -> None:
    """Run the catalog server."""
    run(__doc__ or "", DEFAULT_PORT, create_server, create_rules)


if __name__ == "__main__":
    main()
