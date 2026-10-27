import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from servers.unity_server import LoyaltyStore, _next_tier, create_server


@pytest.fixture
def store(tmp_path: Path) -> LoyaltyStore:
    return LoyaltyStore(tmp_path / "nested" / "unity.db")


def test_balance_is_the_sum_of_activity(store: LoyaltyStore) -> None:
    balance = store.balance(" hr-100245")
    assert (balance.member_id, balance.name, balance.tier) == ("HR-100245", "Alex Morgan", "Legend")
    assert balance.points_balance == 2_500 + 6_120 + 1_480 + 11_370 - 3_020
    assert (balance.next_tier, balance.tier_credits_to_next_tier) == ("Icon", 50_000 - 24_300)
    assert balance.tier_progress == "24,300 tier credits; 25,700 more needed for Icon"


def test_top_earnable_tier_has_no_next_tier(store: LoyaltyStore) -> None:
    balance = store.balance("HR-100512")
    assert (balance.tier, balance.next_tier, balance.tier_credits_to_next_tier) == ("Icon", None, None)
    assert balance.tier_progress == "61,200 tier credits; top earnable tier reached"


@pytest.mark.parametrize(
    ("credits", "expected"), [(0, ("Legend", 10_000)), (9_999, ("Legend", 1)), (10_000, ("Icon", 40_000))]
)
def test_next_tier_thresholds(credits: int, expected: tuple[str, int]) -> None:
    assert _next_tier(credits) == expected


def test_activity_is_newest_first_and_limited(store: LoyaltyStore) -> None:
    activity = store.activity("HR-100245", limit=2)
    assert [(entry.date, entry.points) for entry in activity] == [("2026-09-02", -3_020), ("2026-08-05", 11_370)]
    assert len(store.activity("HR-100245", limit=0)) == 1
    assert len(store.activity("HR-100245", limit=500)) == 5


def test_unknown_member_is_rejected(store: LoyaltyStore) -> None:
    with pytest.raises(ValueError, match="unknown Unity member id"):
        store.balance("HR-1")
    with pytest.raises(ValueError, match="unknown Unity member id"):
        store.activity("HR-1")


def test_seed_runs_once(tmp_path: Path) -> None:
    path = tmp_path / "u.db"
    LoyaltyStore(path)
    assert len(LoyaltyStore(path).activity("HR-100245", limit=50)) == 5


async def test_server_exposes_two_tools(store: LoyaltyStore) -> None:
    tools = await create_server(store).list_tools()
    assert sorted(tool.name for tool in tools) == ["get_points_activity", "get_unity_balance"]


async def test_server_round_trip(store: LoyaltyStore) -> None:
    server = create_server(store)
    _, balance = await server.call_tool("get_unity_balance", {"member_id": "HR-100377"})
    assert (balance["tier"], balance["points_balance"]) == ("Star", 3_140)
    _, activity = await server.call_tool("get_points_activity", {"member_id": "hr-100377", "limit": 1})
    assert activity == {
        "member_id": "HR-100377",
        "activity": [{"date": "2026-07-20", "description": "Retail: hotel gift shop", "points": 640}],
    }


async def test_server_reports_unknown_member_as_tool_error(store: LoyaltyStore) -> None:
    with pytest.raises(ToolError, match="unknown Unity member id"):
        await create_server(store).call_tool("get_unity_balance", {"member_id": "nobody"})


def test_load_test_members_have_their_own_points(store: LoyaltyStore) -> None:
    first = store.balance("HR-300001")
    assert (first.name, first.points_balance) == ("Load Test 01", 2_500 + 1_050)
    assert store.balance("HR-300050").points_balance == 2_500 + 1_000 + 50 * 50


def test_members_added_later_reach_an_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "u.db"
    LoyaltyStore(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM points_activity WHERE member_id LIKE 'HR-3%'")
        conn.execute("DELETE FROM members WHERE member_id LIKE 'HR-3%'")
    reopened = LoyaltyStore(path)
    assert reopened.balance("HR-300007").points_balance == 2_500 + 1_000 + 7 * 50
    assert len(reopened.activity("HR-100245", limit=50)) == 5  # existing members are not re-seeded
