from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from servers.reservations_server import BookingStore, create_server, random_confirmation_number

TODAY = date(2026, 10, 26)
ALEX = "HR-100245"
PRIYA = "HR-100377"


def numbers(*codes: str) -> Iterator[str]:
    yield from codes


@pytest.fixture
def store(tmp_path: Path) -> BookingStore:
    return BookingStore(tmp_path / "nested" / "reservations.db", today=lambda: TODAY)


def test_seeded_bookings_get_a_derived_status(store: BookingStore) -> None:
    bookings = {b.confirmation_number: b.status for b in store.list(ALEX)}
    assert bookings == {"HRM-2H6P4R": "cancelled", "HRM-4M8T2Q": "upcoming", "HRM-9C3K7W": "completed"}


def test_list_is_latest_check_in_first_and_filters_by_status(store: BookingStore) -> None:
    assert [b.check_in for b in store.list(ALEX)] == ["2026-12-28", "2026-11-20", "2026-08-02"]
    assert [b.confirmation_number for b in store.list(ALEX, "upcoming")] == ["HRM-4M8T2Q"]


def test_in_house_status_while_the_stay_is_running(tmp_path: Path) -> None:
    store = BookingStore(tmp_path / "r.db", today=lambda: date(2026, 11, 21))
    assert store.get(ALEX, "HRM-4M8T2Q").status == "in_house"


def test_booking_totals_are_rate_times_nights(store: BookingStore) -> None:
    booking = store.get(ALEX, "hrm-4m8t2q ")
    assert (booking.nights, booking.total_usd, booking.room_name) == (3, 3 * 289, "Deluxe King")


def test_get_hides_other_members_bookings(store: BookingStore) -> None:
    with pytest.raises(ValueError, match=f"member {ALEX} has no booking HRM-7V5N3J"):
        store.get(ALEX, "HRM-7V5N3J")


def test_availability_subtracts_overlapping_confirmed_bookings(store: BookingStore) -> None:
    result = store.availability("2026-12-12", "2026-12-14", 2)
    offers = {offer.room_code: offer.rooms_left for offer in result.available}
    # HRM-7V5N3J holds one DLXQ; HRM-6X2D8L holds the only Rock Star Suite.
    assert offers == {"DLXK": 4, "DLXQ": 3, "BAYK": 2}
    assert (result.nights, result.available[0].total_usd) == (2, 2 * 289)


def test_availability_filters_rooms_too_small_for_the_party(store: BookingStore) -> None:
    result = store.availability("2026-11-01", "2026-11-02", 3)
    assert [offer.room_code for offer in result.available] == ["DLXQ", "RSTR"]


def test_availability_counts_the_busiest_night_only(store: BookingStore) -> None:
    store.create(PRIYA, "BAYK", "2026-11-10", "2026-11-11", 1)
    store.create(PRIYA, "BAYK", "2026-11-11", "2026-11-12", 1)
    offers = {o.room_code: o.rooms_left for o in store.availability("2026-11-10", "2026-11-12", 1).available}
    assert offers["BAYK"] == 1  # one room taken each night, never two on the same night


@pytest.mark.parametrize(
    ("check_in", "check_out", "guests", "message"),
    [
        ("2026-10-25", "2026-10-27", 2, "in the past"),
        ("2026-12-14", "2026-12-12", 2, "check_out must be after check_in"),
        ("2026-12-01", "2026-12-16", 2, "at most 14 nights"),
        ("2027-12-01", "2027-12-02", 2, "at most 365 days ahead"),
        ("2026-12-01", "2026-12-02", 0, "guests must be at least 1"),
        ("Dec 12", "2026-12-14", 2, "check_in must be a date"),
    ],
)
def test_invalid_stays_are_rejected(
    store: BookingStore, check_in: str, check_out: str, guests: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        store.availability(check_in, check_out, guests)


def test_create_books_and_lists_the_new_booking(tmp_path: Path) -> None:
    codes = numbers("HRM-AAAAAA")
    store = BookingStore(tmp_path / "r.db", today=lambda: TODAY, new_confirmation_number=lambda: next(codes))
    booking = store.create(ALEX, " dlxk ", "2026-12-12", "2026-12-14", 2, "  Quiet room ")
    assert booking.confirmation_number == "HRM-AAAAAA"
    assert (booking.room_code, booking.status, booking.special_requests) == ("DLXK", "upcoming", "Quiet room")
    assert "HRM-AAAAAA" in [b.confirmation_number for b in store.list(ALEX, "upcoming")]


def test_create_retries_on_confirmation_number_collision(tmp_path: Path) -> None:
    codes = numbers("HRM-4M8T2Q", "HRM-BBBBBB")
    store = BookingStore(tmp_path / "r.db", today=lambda: TODAY, new_confirmation_number=lambda: next(codes))
    assert store.create(ALEX, "DLXK", "2026-12-12", "2026-12-13", 1).confirmation_number == "HRM-BBBBBB"


def test_create_refuses_a_full_room_type(store: BookingStore) -> None:
    with pytest.raises(ValueError, match="Rock Star Suite is fully booked"):
        store.create(ALEX, "RSTR", "2026-12-13", "2026-12-14", 2)


@pytest.mark.parametrize(
    ("member", "room", "guests", "message"),
    [
        ("HR-000000", "DLXK", 2, "unknown Unity member id"),
        (ALEX, "PENT", 2, "unknown room code 'PENT'"),
        (ALEX, "DLXK", 3, "Deluxe King holds at most 2 guests"),
    ],
)
def test_create_validates_inputs(store: BookingStore, member: str, room: str, guests: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        store.create(member, room, "2026-12-01", "2026-12-02", guests)


def test_cancel_frees_the_room(store: BookingStore) -> None:
    before = {o.room_code: o.rooms_left for o in store.availability("2026-11-20", "2026-11-21", 1).available}
    assert store.cancel(ALEX, "HRM-4M8T2Q").status == "cancelled"
    after = {o.room_code: o.rooms_left for o in store.availability("2026-11-20", "2026-11-21", 1).available}
    assert after["DLXK"] == before["DLXK"] + 1


@pytest.mark.parametrize(("number", "message"), [("HRM-2H6P4R", "is cancelled"), ("HRM-9C3K7W", "is completed")])
def test_cancel_only_upcoming_bookings(store: BookingStore, number: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        store.cancel(ALEX, number)


def test_cancel_cannot_touch_another_members_booking(store: BookingStore) -> None:
    with pytest.raises(ValueError, match="has no booking"):
        store.cancel(ALEX, "HRM-7V5N3J")
    assert store.get(PRIYA, "HRM-7V5N3J").status == "upcoming"


def test_seed_runs_once_and_data_persists(tmp_path: Path) -> None:
    path = tmp_path / "r.db"
    BookingStore(path, today=lambda: TODAY).create(ALEX, "DLXK", "2026-12-12", "2026-12-13", 1)
    assert len(BookingStore(path, today=lambda: TODAY).list(ALEX)) == 4


def test_random_confirmation_numbers_are_unambiguous() -> None:
    number = random_confirmation_number()
    assert number.startswith("HRM-") and len(number) == 10
    assert not set(number[4:]) & set("01IO")


async def test_server_exposes_five_tools(store: BookingStore) -> None:
    tools = await create_server(store).list_tools()
    assert sorted(tool.name for tool in tools) == [
        "cancel_booking",
        "check_availability",
        "create_booking",
        "get_booking",
        "list_bookings",
    ]


async def test_server_round_trip(store: BookingStore) -> None:
    server = create_server(store)
    _, free = await server.call_tool("check_availability", {"check_in": "2026-12-12", "check_out": "2026-12-14"})
    assert free["guests"] == 2 and free["available"]
    _, booked = await server.call_tool(
        "create_booking",
        {"member_id": ALEX, "room_code": "BAYK", "check_in": "2026-12-12", "check_out": "2026-12-14", "guests": 2},
    )
    number = booked["confirmation_number"]
    _, fetched = await server.call_tool("get_booking", {"member_id": ALEX, "confirmation_number": number})
    assert fetched["total_usd"] == 2 * 379
    _, listed = await server.call_tool("list_bookings", {"member_id": ALEX, "status": "upcoming"})
    assert number in [b["confirmation_number"] for b in listed["bookings"]]
    _, cancelled = await server.call_tool("cancel_booking", {"member_id": ALEX, "confirmation_number": number})
    assert cancelled["status"] == "cancelled"


async def test_server_reports_validation_as_tool_error(store: BookingStore) -> None:
    with pytest.raises(ToolError, match="check_out must be after check_in"):
        await create_server(store).call_tool(
            "check_availability", {"check_in": "2026-12-14", "check_out": "2026-12-12"}
        )
