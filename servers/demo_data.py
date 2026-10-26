"""Made-up Hard Rock Hotel Miami data for the demo. None of it is real hotel, guest or loyalty information."""

from __future__ import annotations

from dataclasses import dataclass

DEMO_NOTICE = "Demo data for a WSO2 agent demo; not real Hard Rock information."

HOTEL_NAME = "Hard Rock Hotel Miami"


@dataclass(frozen=True)
class RoomType:
    """A bookable room type. `inventory` is how many rooms of this type exist per night."""

    code: str
    name: str
    description: str
    max_guests: int
    nightly_rate_usd: int
    inventory: int


ROOM_TYPES: tuple[RoomType, ...] = (
    RoomType("DLXK", "Deluxe King", "One king bed, city view", 2, 289, 4),
    RoomType("DLXQ", "Deluxe Two Queens", "Two queen beds, city view", 4, 309, 4),
    RoomType("BAYK", "Bay View King", "One king bed, Biscayne Bay view, balcony", 2, 379, 2),
    RoomType("RSTR", "Rock Star Suite", "Separate living room, bay view, sound system", 4, 649, 1),
)

ROOMS_BY_CODE = {room.code: room for room in ROOM_TYPES}


@dataclass(frozen=True)
class Member:
    """A Unity by Hard Rock member."""

    member_id: str
    name: str
    tier: str
    tier_credits: int


MEMBERS: tuple[Member, ...] = (
    Member("HR-100245", "Alex Morgan", "Legend", 24_300),
    Member("HR-100377", "Priya Natarajan", "Star", 2_300),
    Member("HR-100512", "Daniel Okafor", "Icon", 61_200),
)

MEMBER_IDS = {member.member_id for member in MEMBERS}

# (confirmation_number, member_id, room_code, check_in, check_out, guests, status, special_requests)
SEED_BOOKINGS: tuple[tuple[str, str, str, str, str, int, str, str], ...] = (
    ("HRM-4M8T2Q", "HR-100245", "DLXK", "2026-11-20", "2026-11-23", 2, "confirmed", "High floor"),
    ("HRM-9C3K7W", "HR-100245", "BAYK", "2026-08-02", "2026-08-05", 2, "confirmed", ""),
    ("HRM-2H6P4R", "HR-100245", "DLXQ", "2026-12-28", "2027-01-02", 3, "cancelled", ""),
    ("HRM-7V5N3J", "HR-100377", "DLXQ", "2026-12-12", "2026-12-14", 4, "confirmed", "Crib"),
    ("HRM-6X2D8L", "HR-100512", "RSTR", "2026-12-12", "2026-12-15", 2, "confirmed", "Late check-out"),
)

# (member_id, date, description, points). A member's balance is the sum of their activity.
SEED_POINTS: tuple[tuple[str, str, str, int], ...] = (
    ("HR-100245", "2026-03-14", "Welcome bonus", 2_500),
    ("HR-100245", "2026-05-09", "Stay: Hard Rock Hotel Miami, 2 nights", 6_120),
    ("HR-100245", "2026-06-21", "Dining: hotel steakhouse", 1_480),
    ("HR-100245", "2026-08-05", "Stay: Hard Rock Hotel Miami, 3 nights", 11_370),
    ("HR-100245", "2026-09-02", "Redeemed: spa credit", -3_020),
    ("HR-100377", "2026-07-18", "Welcome bonus", 2_500),
    ("HR-100377", "2026-07-20", "Retail: hotel gift shop", 640),
    ("HR-100512", "2025-11-30", "Stay: Hard Rock Hotel Miami, 5 nights", 41_250),
    ("HR-100512", "2026-02-14", "Entertainment: concert package", 18_900),
    ("HR-100512", "2026-06-01", "Redeemed: suite upgrade", -12_000),
)

# Tier credits needed to reach each tier. X is invitation-only, so it has no threshold.
TIER_THRESHOLDS: tuple[tuple[str, int], ...] = (("Star", 0), ("Legend", 10_000), ("Icon", 50_000))
