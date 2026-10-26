"""Reservations MCP server: room availability and Unity members' bookings at Hard Rock Hotel Miami, in SQLite.

Runs standalone over Streamable HTTP (the transport WSO2 AI gateways proxy) or stdio:

    python servers/reservations_server.py                 # http://127.0.0.1:8102/mcp
    python servers/reservations_server.py --transport stdio
    RESERVATIONS_DB_PATH=/tmp/reservations.db python servers/reservations_server.py

The database is created and seeded with demo bookings on first start.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from servers._common import base_parser, new_server, parse_date, require_member
from servers.demo_data import MEMBER_IDS, ROOM_TYPES, ROOMS_BY_CODE, SEED_BOOKINGS, RoomType

SERVER_NAME = "reservations"
DEFAULT_PORT = 8102
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "reservations.db"
MAX_NIGHTS = 14
MAX_DAYS_AHEAD = 365
CONFIRMATION_PREFIX = "HRM-"
CONFIRMATION_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O or 1/I: read aloud by guests
MAX_CODE_ATTEMPTS = 5

StayStatus = Literal["upcoming", "in_house", "completed", "cancelled"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    confirmation_number TEXT PRIMARY KEY,
    member_id           TEXT NOT NULL,
    room_code           TEXT NOT NULL,
    check_in            TEXT NOT NULL,
    check_out           TEXT NOT NULL,
    guests              INTEGER NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    special_requests    TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class Booking:
    """A booking as returned to the agent. `status` is derived from today's date for confirmed stays."""

    confirmation_number: str
    member_id: str
    room_code: str
    room_name: str
    check_in: str
    check_out: str
    nights: int
    guests: int
    total_usd: int
    status: StayStatus
    special_requests: str


@dataclass(frozen=True)
class BookingList:
    """Tool result wrapper: MCP structured content must be a JSON object, not a bare list."""

    bookings: list[Booking]


@dataclass(frozen=True)
class RoomOffer:
    """A room type that is free for every night of the requested stay."""

    room_code: str
    room_name: str
    max_guests: int
    rooms_left: int
    nightly_rate_usd: int
    total_usd: int


@dataclass(frozen=True)
class Availability:
    """What can be booked for one stay. An empty `available` list means the hotel is full for those dates."""

    check_in: str
    check_out: str
    nights: int
    guests: int
    available: list[RoomOffer]


def random_confirmation_number() -> str:
    """A fresh `HRM-XXXXXX` confirmation number."""
    return CONFIRMATION_PREFIX + "".join(secrets.choice(CONFIRMATION_ALPHABET) for _ in range(6))


class BookingStore:
    """Availability and booking operations. Opens a short-lived connection per call."""

    def __init__(
        self,
        db_path: Path,
        today: Callable[[], date] = date.today,
        new_confirmation_number: Callable[[], str] = random_confirmation_number,
    ) -> None:
        self._db_path = db_path
        self._today = today
        self._new_confirmation_number = new_confirmation_number
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, _transaction(conn):
            conn.execute(_SCHEMA)
            if conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0:
                created_at = _now()
                conn.executemany(
                    "INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(*row, created_at) for row in SEED_BOOKINGS],
                )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def availability(self, check_in: str, check_out: str, guests: int) -> Availability:
        """Room types with at least one room free on every night of the stay and room for `guests`.

        Raises:
            ValueError: if the dates or guest count are invalid.
        """
        start, end = self._validate_stay(check_in, check_out, guests)
        nights = (end - start).days
        with closing(self._connect()) as conn:
            offers = []
            for room in ROOM_TYPES:
                if room.max_guests < guests:
                    continue
                left = _rooms_left(conn, room, start, end)
                if left > 0:
                    offers.append(
                        RoomOffer(
                            room.code,
                            room.name,
                            room.max_guests,
                            left,
                            room.nightly_rate_usd,
                            room.nightly_rate_usd * nights,
                        )
                    )
        return Availability(start.isoformat(), end.isoformat(), nights, guests, offers)

    def list(self, member_id: str, status: StayStatus | None = None) -> list[Booking]:
        """A member's bookings, latest check-in first, optionally only those with `status`.

        Raises:
            ValueError: if the member id is unknown.
        """
        member = require_member(member_id, MEMBER_IDS)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM bookings WHERE member_id = ? ORDER BY check_in DESC", (member,)
            ).fetchall()
        bookings = [self._to_booking(row) for row in rows]
        return [booking for booking in bookings if status is None or booking.status == status]

    def get(self, member_id: str, confirmation_number: str) -> Booking:
        """One of the member's bookings.

        Raises:
            ValueError: if the member is unknown or has no booking with that number.
        """
        member = require_member(member_id, MEMBER_IDS)
        with closing(self._connect()) as conn:
            return self._to_booking(_owned_row(conn, member, confirmation_number))

    def create(
        self,
        member_id: str,
        room_code: str,
        check_in: str,
        check_out: str,
        guests: int,
        special_requests: str = "",
    ) -> Booking:
        """Book one room, checking availability and inserting in a single write transaction.

        Raises:
            ValueError: if any input is invalid or the room type is full for those dates.
        """
        member = require_member(member_id, MEMBER_IDS)
        room = ROOMS_BY_CODE.get(room_code.strip().upper())
        if room is None:
            raise ValueError(f"unknown room code {room_code!r}; valid codes: {', '.join(ROOMS_BY_CODE)}")
        start, end = self._validate_stay(check_in, check_out, guests)
        if guests > room.max_guests:
            raise ValueError(f"{room.name} holds at most {room.max_guests} guests; {guests} requested")
        with closing(self._connect()) as conn, _transaction(conn):
            if _rooms_left(conn, room, start, end) <= 0:
                raise ValueError(f"{room.name} is fully booked for {start} to {end}; check availability first")
            number = self._insert(conn, member, room, start, end, guests, special_requests.strip())
            return self._to_booking(_owned_row(conn, member, number))

    def cancel(self, member_id: str, confirmation_number: str) -> Booking:
        """Cancel one of the member's upcoming bookings.

        Raises:
            ValueError: if the booking does not exist, is already cancelled, or has started.
        """
        member = require_member(member_id, MEMBER_IDS)
        with closing(self._connect()) as conn, _transaction(conn):
            booking = self._to_booking(_owned_row(conn, member, confirmation_number))
            if booking.status != "upcoming":
                raise ValueError(f"booking {booking.confirmation_number} is {booking.status} and cannot be cancelled")
            conn.execute(
                "UPDATE bookings SET status = 'cancelled' WHERE confirmation_number = ?",
                (booking.confirmation_number,),
            )
            return self._to_booking(_owned_row(conn, member, booking.confirmation_number))

    def _insert(
        self,
        conn: sqlite3.Connection,
        member: str,
        room: RoomType,
        start: date,
        end: date,
        guests: int,
        special_requests: str,
    ) -> str:
        for _ in range(MAX_CODE_ATTEMPTS):
            number = self._new_confirmation_number()
            try:
                conn.execute(
                    "INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
                    (number, member, room.code, start.isoformat(), end.isoformat(), guests, special_requests, _now()),
                )
            except sqlite3.IntegrityError:
                continue  # confirmation number collision; draw another
            return number
        raise RuntimeError(f"no unused confirmation number after {MAX_CODE_ATTEMPTS} attempts")

    def _validate_stay(self, check_in: str, check_out: str, guests: int) -> tuple[date, date]:
        start, end = parse_date(check_in, "check_in"), parse_date(check_out, "check_out")
        today = self._today()
        if start < today:
            raise ValueError(f"check_in {start} is in the past; today is {today}")
        if start > today + timedelta(days=MAX_DAYS_AHEAD):
            raise ValueError(f"bookings open at most {MAX_DAYS_AHEAD} days ahead")
        if end <= start:
            raise ValueError("check_out must be after check_in")
        if (end - start).days > MAX_NIGHTS:
            raise ValueError(f"a stay can be at most {MAX_NIGHTS} nights")
        if guests < 1:
            raise ValueError("guests must be at least 1")
        return start, end

    def _to_booking(self, row: sqlite3.Row) -> Booking:
        room = ROOMS_BY_CODE[row["room_code"]]
        start, end = date.fromisoformat(row["check_in"]), date.fromisoformat(row["check_out"])
        nights = (end - start).days
        return Booking(
            confirmation_number=row["confirmation_number"],
            member_id=row["member_id"],
            room_code=room.code,
            room_name=room.name,
            check_in=row["check_in"],
            check_out=row["check_out"],
            nights=nights,
            guests=row["guests"],
            total_usd=room.nightly_rate_usd * nights,
            status=_stay_status(row["status"], start, end, self._today()),
            special_requests=row["special_requests"],
        )


def _stay_status(stored: str, start: date, end: date, today: date) -> StayStatus:
    if stored == "cancelled":
        return "cancelled"
    if today < start:
        return "upcoming"
    return "in_house" if today < end else "completed"


def _rooms_left(conn: sqlite3.Connection, room: RoomType, start: date, end: date) -> int:
    """Rooms of this type free on the busiest night of [start, end)."""
    overlapping = conn.execute(
        "SELECT check_in, check_out FROM bookings "
        "WHERE room_code = ? AND status = 'confirmed' AND check_in < ? AND check_out > ?",
        (room.code, end.isoformat(), start.isoformat()),
    ).fetchall()
    nights = (start + timedelta(days=offset) for offset in range((end - start).days))
    busiest = max(
        (sum(1 for row in overlapping if row["check_in"] <= night.isoformat() < row["check_out"]) for night in nights),
        default=0,
    )
    return room.inventory - busiest


def _owned_row(conn: sqlite3.Connection, member: str, confirmation_number: str) -> sqlite3.Row:
    """The booking row if it belongs to `member`; the error does not reveal other members' bookings."""
    number = confirmation_number.strip().upper()
    row = conn.execute(
        "SELECT * FROM bookings WHERE confirmation_number = ? AND member_id = ?", (number, member)
    ).fetchone()
    if row is None:
        raise ValueError(f"member {member} has no booking {number}")
    return row


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE so the availability check and the insert can't interleave with another writer."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def create_server(
    store: BookingStore,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    allowed_hosts: list[str] | None = None,
) -> FastMCP:
    """Build the reservations server with its five tools registered against `store`."""
    server = new_server(
        SERVER_NAME,
        "Room availability and bookings at Hard Rock Hotel Miami for Unity by Hard Rock members.",
        host,
        port,
        allowed_hosts,
    )

    @server.tool()
    def check_availability(check_in: str, check_out: str, guests: int = 2) -> Availability:
        """Find room types free for a stay. Dates are YYYY-MM-DD; check_out is the departure day."""
        return store.availability(check_in, check_out, guests)

    @server.tool()
    def list_bookings(member_id: str, status: StayStatus | None = None) -> BookingList:
        """List a Unity member's bookings, latest first. Optionally filter by status, e.g. 'upcoming'."""
        return BookingList(store.list(member_id, status))

    @server.tool()
    def get_booking(member_id: str, confirmation_number: str) -> Booking:
        """Get one of the member's bookings by its confirmation number, e.g. HRM-4M8T2Q."""
        return store.get(member_id, confirmation_number)

    @server.tool()
    def create_booking(
        member_id: str,
        room_code: str,
        check_in: str,
        check_out: str,
        guests: int,
        special_requests: str = "",
    ) -> Booking:
        """Book one room for the member. room_code comes from check_availability, e.g. DLXK. Dates are YYYY-MM-DD."""
        return store.create(member_id, room_code, check_in, check_out, guests, special_requests)

    @server.tool()
    def cancel_booking(member_id: str, confirmation_number: str) -> Booking:
        """Cancel one of the member's upcoming bookings. Only do this when the guest clearly asks to cancel."""
        return store.cancel(member_id, confirmation_number)

    return server


def main() -> None:
    """Parse CLI flags and run the server."""
    parser = base_parser(__doc__, DEFAULT_PORT)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("RESERVATIONS_DB_PATH", DEFAULT_DB_PATH)))
    args = parser.parse_args()
    create_server(BookingStore(args.db), args.host, args.port, args.allowed_host).run(transport=args.transport)


if __name__ == "__main__":
    main()
