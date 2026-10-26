"""SQLite-backed hotel data shared by the four StayWise MCP servers.

Every server opens the same database file; each call uses a short-lived connection, so the
servers can run as separate processes. The first open creates the schema and seeds a few
fictional Sri Lankan hotels.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(
    os.environ.get("STAYWISE_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "staywise.db")
)
DEPOSIT_RATE = 0.25
WELCOME_POINTS = 12_500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hotels (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL, stars INTEGER NOT NULL, description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY, hotel_id TEXT NOT NULL REFERENCES hotels(id), name TEXT NOT NULL,
    nightly_rate REAL NOT NULL, sea_view INTEGER NOT NULL, refundable INTEGER NOT NULL,
    max_guests INTEGER NOT NULL, inventory INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY, guest_sub TEXT NOT NULL, room_id TEXT NOT NULL REFERENCES rooms(id),
    check_in TEXT NOT NULL, check_out TEXT NOT NULL, guests INTEGER NOT NULL, status TEXT NOT NULL,
    total REAL NOT NULL, paid REAL NOT NULL DEFAULT 0, created_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY, reservation_id TEXT NOT NULL REFERENCES reservations(id), amount REAL NOT NULL,
    purpose TEXT NOT NULL, method TEXT NOT NULL, authorized_client TEXT NOT NULL, acting_agent TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loyalty (guest_sub TEXT PRIMARY KEY, points INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ops_requests (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, reservation_id TEXT, guest_name TEXT, message TEXT NOT NULL,
    requested_by TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

_HOTELS = [
    (
        "lighthouse-bay",
        "Lighthouse Bay Hotel",
        "Galle",
        5,
        "Clifftop hotel beside the Galle lighthouse, every room faces the ocean.",
    ),
    ("fort-ramparts", "Fort Ramparts Inn", "Galle", 4, "Boutique inn inside Galle Fort, steps from the ramparts walk."),
    ("marine-drive", "Marine Drive Suites", "Colombo", 4, "Modern serviced suites on Colombo's seafront Marine Drive."),
    (
        "lakeside-kandy",
        "Lakeside Kandy Residence",
        "Kandy",
        4,
        "Colonial villa overlooking Kandy Lake and the Temple of the Tooth.",
    ),
    ("nine-arches", "Nine Arches Lodge", "Ella", 3, "Hill-country cabins with views of the Nine Arches Bridge."),
]

# id, hotel, name, nightly rate, sea view, refundable, max guests, inventory
_ROOMS = [
    ("lighthouse-deluxe", "lighthouse-bay", "Lighthouse Deluxe", 180.0, 1, 1, 2, 4),
    ("ocean-suite", "lighthouse-bay", "Ocean Suite", 620.0, 1, 0, 3, 1),
    ("garden-room", "lighthouse-bay", "Garden Room", 110.0, 0, 1, 2, 6),
    ("rampart-double", "fort-ramparts", "Rampart Double", 150.0, 1, 1, 2, 3),
    ("courtyard-single", "fort-ramparts", "Courtyard Single", 85.0, 0, 1, 1, 2),
    ("sea-studio", "marine-drive", "Sea Studio", 165.0, 1, 1, 2, 5),
    ("city-studio", "marine-drive", "City Studio", 120.0, 0, 1, 2, 5),
    ("lake-view", "lakeside-kandy", "Lake View Room", 140.0, 0, 1, 2, 4),
    ("hill-cabin", "nine-arches", "Hill Cabin", 95.0, 0, 1, 3, 3),
]


class StoreError(ValueError):
    """A request the data cannot satisfy (unknown id, bad dates, sold out, ...)."""


@dataclass(frozen=True)
class Room:
    """A bookable room type with its hotel details folded in."""

    id: str
    hotel_id: str
    hotel_name: str
    city: str
    name: str
    nightly_rate: float
    sea_view: bool
    refundable: bool
    max_guests: int


@dataclass(frozen=True)
class Reservation:
    """A guest's booking. `balance_due` is `total - paid`."""

    id: str
    room_id: str
    room_name: str
    hotel_name: str
    check_in: str
    check_out: str
    nights: int
    guests: int
    status: str
    refundable: bool
    total: float
    paid: float
    balance_due: float
    deposit_due: float


@dataclass(frozen=True)
class Payment:
    """A recorded charge, including who authorized it."""

    id: str
    reservation_id: str
    amount: float
    purpose: str
    method: str
    authorized_client: str
    acting_agent: str | None
    created_at: str


def to_dict(value: Any) -> dict[str, Any]:
    """Dataclass to plain dict, for tool results."""
    return asdict(value)


def parse_dates(check_in: str, check_out: str) -> tuple[date, date, int]:
    """Validate an ISO date range and return it with its night count.

    Raises:
        StoreError: if a date is malformed or the stay is not at least one night.
    """
    try:
        start, end = date.fromisoformat(check_in), date.fromisoformat(check_out)
    except ValueError as exc:
        raise StoreError(f"dates must be YYYY-MM-DD: {exc}") from exc
    nights = (end - start).days
    if nights < 1:
        raise StoreError("check_out must be at least one day after check_in")
    return start, end, nights


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class HotelStore:
    """All data access for the StayWise servers."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)
            if conn.execute("SELECT COUNT(*) FROM hotels").fetchone()[0] == 0:
                conn.executemany("INSERT INTO hotels VALUES (?, ?, ?, ?, ?)", _HOTELS)
                conn.executemany("INSERT INTO rooms VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _ROOMS)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ---- catalog -------------------------------------------------------------------------

    def search_rooms(
        self, city: str | None = None, max_nightly_rate: float | None = None, sea_view: bool | None = None
    ) -> list[Room]:
        """Room types matching every given filter, cheapest first."""
        sql = "SELECT r.*, h.name AS hotel_name, h.city FROM rooms r JOIN hotels h ON h.id = r.hotel_id WHERE 1=1"
        params: list[Any] = []
        if city:
            sql += " AND lower(h.city) = lower(?)"
            params.append(city.strip())
        if max_nightly_rate is not None:
            sql += " AND r.nightly_rate <= ?"
            params.append(max_nightly_rate)
        if sea_view is not None:
            sql += " AND r.sea_view = ?"
            params.append(int(sea_view))
        with closing(self._connect()) as conn:
            return [_room(row) for row in conn.execute(sql + " ORDER BY r.nightly_rate", params)]

    def get_room(self, room_id: str) -> Room:
        """Look up one room type.

        Raises:
            StoreError: if the id is unknown.
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT r.*, h.name AS hotel_name, h.city FROM rooms r JOIN hotels h ON h.id = r.hotel_id"
                " WHERE r.id = ?",
                (room_id,),
            ).fetchone()
        if row is None:
            raise StoreError(f"no room with id {room_id!r}")
        return _room(row)

    def rooms_left(self, room_id: str, check_in: str, check_out: str) -> int:
        """How many of a room type are still free for every night of the stay."""
        room = self.get_room(room_id)
        parse_dates(check_in, check_out)
        with closing(self._connect()) as conn:
            inventory = conn.execute("SELECT inventory FROM rooms WHERE id = ?", (room.id,)).fetchone()[0]
            overlapping = conn.execute(
                "SELECT COUNT(*) FROM reservations WHERE room_id = ? AND status != 'cancelled'"
                " AND check_in < ? AND check_out > ?",
                (room_id, check_out, check_in),
            ).fetchone()[0]
        return max(inventory - overlapping, 0)

    # ---- reservations --------------------------------------------------------------------

    def create_reservation(
        self, guest_sub: str, room_id: str, check_in: str, check_out: str, guests: int, created_by: str
    ) -> Reservation:
        """Book a room for a guest.

        Raises:
            StoreError: on bad dates, too many guests, or no rooms left.
        """
        room = self.get_room(room_id)
        _, _, nights = parse_dates(check_in, check_out)
        if not 1 <= guests <= room.max_guests:
            raise StoreError(f"{room.name} sleeps 1 to {room.max_guests} guests")
        if self.rooms_left(room_id, check_in, check_out) < 1:
            raise StoreError(f"{room.name} is sold out for those dates")
        reservation_id = f"SW-{uuid.uuid4().hex[:6].upper()}"
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?, 0, ?, ?)",
                (
                    reservation_id,
                    guest_sub,
                    room_id,
                    check_in,
                    check_out,
                    guests,
                    room.nightly_rate * nights,
                    created_by,
                    _now(),
                ),
            )
        return self.get_reservation(guest_sub, reservation_id)

    def get_reservation(self, guest_sub: str, reservation_id: str) -> Reservation:
        """One of the guest's reservations.

        Raises:
            StoreError: if it does not exist or belongs to someone else.
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                _RESERVATION_SQL + " WHERE v.id = ? AND v.guest_sub = ?", (reservation_id, guest_sub)
            ).fetchone()
        if row is None:
            raise StoreError(f"no reservation {reservation_id!r} for this guest")
        return _reservation(row)

    def find_reservation(self, reservation_id: str) -> Reservation | None:
        """A reservation by id regardless of owner; used by the auth guard's rules."""
        with closing(self._connect()) as conn:
            row = conn.execute(_RESERVATION_SQL + " WHERE v.id = ?", (reservation_id,)).fetchone()
        return None if row is None else _reservation(row)

    def list_reservations(self, guest_sub: str) -> list[Reservation]:
        """The guest's reservations, newest stay first."""
        with closing(self._connect()) as conn:
            rows = conn.execute(_RESERVATION_SQL + " WHERE v.guest_sub = ? ORDER BY v.check_in DESC", (guest_sub,))
            return [_reservation(row) for row in rows]

    def change_room(self, guest_sub: str, reservation_id: str, new_room_id: str) -> Reservation:
        """Move a confirmed reservation to another room type in the same hotel, repricing it.

        Raises:
            StoreError: if the reservation is not active, the room is in another hotel, or sold out.
        """
        current = self.get_reservation(guest_sub, reservation_id)
        if current.status != "confirmed":
            raise StoreError(f"reservation {reservation_id} is {current.status}")
        old_room, new_room = self.get_room(current.room_id), self.get_room(new_room_id)
        if new_room.hotel_id != old_room.hotel_id:
            raise StoreError("room changes must stay within the same hotel")
        if self.rooms_left(new_room_id, current.check_in, current.check_out) < 1:
            raise StoreError(f"{new_room.name} is sold out for those dates")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE reservations SET room_id = ?, total = ? WHERE id = ?",
                (new_room_id, new_room.nightly_rate * current.nights, reservation_id),
            )
        return self.get_reservation(guest_sub, reservation_id)

    def cancel_reservation(self, guest_sub: str, reservation_id: str) -> Reservation:
        """Cancel a reservation. Payments on non-refundable rates are kept.

        Raises:
            StoreError: if it is already cancelled.
        """
        current = self.get_reservation(guest_sub, reservation_id)
        if current.status == "cancelled":
            raise StoreError(f"reservation {reservation_id} is already cancelled")
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE reservations SET status = 'cancelled' WHERE id = ?", (reservation_id,))
        return self.get_reservation(guest_sub, reservation_id)

    # ---- payments & loyalty --------------------------------------------------------------

    def record_payment(
        self,
        guest_sub: str,
        reservation_id: str,
        amount: float,
        purpose: str,
        method: str,
        authorized_client: str,
        acting_agent: str | None,
    ) -> Payment:
        """Charge an amount against a reservation's balance.

        Raises:
            StoreError: if the amount is not positive or exceeds the balance due.
        """
        reservation = self.get_reservation(guest_sub, reservation_id)
        if amount <= 0:
            raise StoreError("amount must be positive")
        if amount > reservation.balance_due + 0.005:
            raise StoreError(f"amount exceeds the balance due ({reservation.balance_due:.2f})")
        payment = Payment(
            f"PAY-{uuid.uuid4().hex[:8].upper()}",
            reservation_id,
            round(amount, 2),
            purpose,
            method,
            authorized_client,
            acting_agent,
            _now(),
        )
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(asdict(payment).values()))
            conn.execute("UPDATE reservations SET paid = paid + ? WHERE id = ?", (payment.amount, reservation_id))
        return payment

    def loyalty_points(self, guest_sub: str) -> int:
        """The guest's points, enrolling new guests with a welcome balance."""
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO loyalty VALUES (?, ?)", (guest_sub, WELCOME_POINTS))
            return conn.execute("SELECT points FROM loyalty WHERE guest_sub = ?", (guest_sub,)).fetchone()[0]

    def redeem_points(self, guest_sub: str, points: int) -> int:
        """Spend points and return the remaining balance.

        Raises:
            StoreError: if points is not positive or exceeds the balance.
        """
        balance = self.loyalty_points(guest_sub)
        if not 0 < points <= balance:
            raise StoreError(f"can redeem between 1 and {balance} points")
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE loyalty SET points = points - ? WHERE guest_sub = ?", (points, guest_sub))
        return balance - points

    # ---- hotel operations ----------------------------------------------------------------

    def add_ops_request(
        self, kind: str, message: str, requested_by: str, reservation_id: str | None, guest_name: str | None
    ) -> dict[str, Any]:
        """Queue a note for hotel staff and return it."""
        request = {
            "id": f"OPS-{uuid.uuid4().hex[:6].upper()}",
            "kind": kind,
            "reservation_id": reservation_id,
            "guest_name": guest_name,
            "message": message,
            "requested_by": requested_by,
            "created_at": _now(),
        }
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO ops_requests VALUES (:id, :kind, :reservation_id, :guest_name, :message,"
                " :requested_by, :created_at)",
                request,
            )
        return request


_RESERVATION_SQL = (
    "SELECT v.*, r.name AS room_name, r.refundable, h.name AS hotel_name FROM reservations v"
    " JOIN rooms r ON r.id = v.room_id JOIN hotels h ON h.id = r.hotel_id"
)


def _room(row: sqlite3.Row) -> Room:
    return Room(
        row["id"],
        row["hotel_id"],
        row["hotel_name"],
        row["city"],
        row["name"],
        row["nightly_rate"],
        bool(row["sea_view"]),
        bool(row["refundable"]),
        row["max_guests"],
    )


def _reservation(row: sqlite3.Row) -> Reservation:
    nights = (date.fromisoformat(row["check_out"]) - date.fromisoformat(row["check_in"])).days
    total, paid = row["total"], row["paid"]
    deposit = round(total * DEPOSIT_RATE, 2) if bool(row["refundable"]) else total
    return Reservation(
        row["id"],
        row["room_id"],
        row["room_name"],
        row["hotel_name"],
        row["check_in"],
        row["check_out"],
        nights,
        row["guests"],
        row["status"],
        bool(row["refundable"]),
        total,
        paid,
        round(total - paid, 2),
        round(max(deposit - paid, 0), 2),
    )
