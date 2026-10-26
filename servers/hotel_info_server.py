"""Hotel info MCP server: public facts about Hard Rock Hotel Miami and its room types.

Runs standalone over Streamable HTTP (the transport WSO2 AI gateways proxy) or stdio:

    python servers/hotel_info_server.py                   # http://127.0.0.1:8101/mcp
    python servers/hotel_info_server.py --transport stdio
"""

from __future__ import annotations

from dataclasses import dataclass

from mcp.server.fastmcp import FastMCP

from servers._common import base_parser, new_server
from servers.demo_data import DEMO_NOTICE, HOTEL_NAME, ROOM_TYPES

SERVER_NAME = "hotel-info"
DEFAULT_PORT = 8101


@dataclass(frozen=True)
class HotelInfo:
    """Property details a guest might ask the concierge about."""

    name: str
    address: str
    check_in_time: str
    check_out_time: str
    amenities: list[str]
    notice: str


@dataclass(frozen=True)
class RoomTypeInfo:
    """A room type as shown to guests (inventory stays private to the reservations server)."""

    code: str
    name: str
    description: str
    max_guests: int
    nightly_rate_usd: int


@dataclass(frozen=True)
class RoomTypeList:
    """Tool result wrapper: MCP structured content must be a JSON object, not a bare list."""

    room_types: list[RoomTypeInfo]


HOTEL = HotelInfo(
    name=HOTEL_NAME,
    address="Biscayne Boulevard, Miami, FL",
    check_in_time="16:00",
    check_out_time="11:00",
    amenities=["Rooftop pool", "Spa", "Fitness center", "Live music lounge", "Steakhouse", "Valet parking"],
    notice=DEMO_NOTICE,
)


def create_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT, allowed_hosts: list[str] | None = None) -> FastMCP:
    """Build the hotel info server with its two read-only tools."""
    server = new_server(
        SERVER_NAME,
        "Public information about Hard Rock Hotel Miami: address, check-in times, amenities and room types.",
        host,
        port,
        allowed_hosts,
    )

    @server.tool()
    def get_hotel_info() -> HotelInfo:
        """Get Hard Rock Hotel Miami's address, check-in and check-out times, and amenities."""
        return HOTEL

    @server.tool()
    def list_room_types() -> RoomTypeList:
        """List the room types with their code, max guests and nightly rate in USD. Use the code when booking."""
        return RoomTypeList(
            [
                RoomTypeInfo(room.code, room.name, room.description, room.max_guests, room.nightly_rate_usd)
                for room in ROOM_TYPES
            ]
        )

    return server


def main() -> None:
    """Parse CLI flags and run the server."""
    args = base_parser(__doc__, DEFAULT_PORT).parse_args()
    create_server(args.host, args.port, args.allowed_host).run(transport=args.transport)


if __name__ == "__main__":
    main()
