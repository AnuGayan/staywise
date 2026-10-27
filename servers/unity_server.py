"""Unity loyalty MCP server: Unity by Hard Rock points balances, tiers and points activity, in SQLite.

Runs standalone over Streamable HTTP (the transport WSO2 AI gateways proxy) or stdio:

    python servers/unity_server.py                        # http://127.0.0.1:8103/mcp
    python servers/unity_server.py --transport stdio
    UNITY_DB_PATH=/tmp/unity.db python servers/unity_server.py

The database is created and seeded with demo members on first start.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from servers._common import base_parser, new_server, require_member
from servers.demo_data import MEMBER_IDS, MEMBERS, SEED_POINTS, TIER_THRESHOLDS

SERVER_NAME = "unity-loyalty"
DEFAULT_PORT = 8103
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "unity.db"
MAX_ACTIVITY = 50

_SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    member_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    tier         TEXT NOT NULL,
    tier_credits INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS points_activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   TEXT NOT NULL REFERENCES members (member_id),
    date        TEXT NOT NULL,
    description TEXT NOT NULL,
    points      INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class UnityBalance:
    """A member's points and tier standing. `next_tier` is None at the top earnable tier.

    Points are spent on rewards; tier credits decide the tier. `tier_progress` spells this out so
    small models don't mix the two numbers up.
    """

    member_id: str
    name: str
    tier: str
    points_balance: int
    tier_credits: int
    next_tier: str | None
    tier_credits_to_next_tier: int | None
    tier_progress: str


@dataclass(frozen=True)
class PointsEntry:
    """One points earn (positive) or redemption (negative)."""

    date: str
    description: str
    points: int


@dataclass(frozen=True)
class PointsActivity:
    """Tool result wrapper: MCP structured content must be a JSON object, not a bare list."""

    member_id: str
    activity: list[PointsEntry]


class LoyaltyStore:
    """Read access to Unity members and their points. Opens a short-lived connection per call."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)
            # Upsert, so members added to the demo data later (e.g. load-test guests) reach existing databases.
            conn.executemany(
                "INSERT OR IGNORE INTO members VALUES (?, ?, ?, ?)",
                [(m.member_id, m.name, m.tier, m.tier_credits) for m in MEMBERS],
            )
            seeded = {row[0] for row in conn.execute("SELECT DISTINCT member_id FROM points_activity")}
            conn.executemany(
                "INSERT INTO points_activity (member_id, date, description, points) VALUES (?, ?, ?, ?)",
                [row for row in SEED_POINTS if row[0] not in seeded],
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def balance(self, member_id: str) -> UnityBalance:
        """The member's points balance (sum of all activity) and tier progress.

        Raises:
            ValueError: if the member id is unknown.
        """
        member_id = require_member(member_id, MEMBER_IDS)
        with closing(self._connect()) as conn:
            member = conn.execute("SELECT * FROM members WHERE member_id = ?", (member_id,)).fetchone()
            points = conn.execute(
                "SELECT COALESCE(SUM(points), 0) FROM points_activity WHERE member_id = ?", (member_id,)
            ).fetchone()[0]
        next_tier, credits_to_go = _next_tier(member["tier_credits"])
        return UnityBalance(
            member_id=member_id,
            name=member["name"],
            tier=member["tier"],
            points_balance=points,
            tier_credits=member["tier_credits"],
            next_tier=next_tier,
            tier_credits_to_next_tier=credits_to_go,
            tier_progress=_tier_progress(member["tier_credits"], next_tier, credits_to_go),
        )

    def activity(self, member_id: str, limit: int = 10) -> list[PointsEntry]:
        """The member's most recent points activity, newest first.

        Raises:
            ValueError: if the member id is unknown.
        """
        member_id = require_member(member_id, MEMBER_IDS)
        limit = max(1, min(limit, MAX_ACTIVITY))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT date, description, points FROM points_activity WHERE member_id = ? "
                "ORDER BY date DESC, id DESC LIMIT ?",
                (member_id, limit),
            ).fetchall()
        return [PointsEntry(row["date"], row["description"], row["points"]) for row in rows]


def _next_tier(tier_credits: int) -> tuple[str | None, int | None]:
    """The first tier whose threshold is above `tier_credits`, and how many credits are missing."""
    for tier, threshold in TIER_THRESHOLDS:
        if tier_credits < threshold:
            return tier, threshold - tier_credits
    return None, None


def _tier_progress(tier_credits: int, next_tier: str | None, credits_to_go: int | None) -> str:
    if next_tier is None or credits_to_go is None:
        return f"{tier_credits:,} tier credits; top earnable tier reached"
    return f"{tier_credits:,} tier credits; {credits_to_go:,} more needed for {next_tier}"


def create_server(
    store: LoyaltyStore,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    allowed_hosts: list[str] | None = None,
) -> FastMCP:
    """Build the Unity loyalty server with its two read-only tools registered against `store`."""
    server = new_server(
        SERVER_NAME,
        "Unity by Hard Rock loyalty accounts: points balance, tier and points activity.",
        host,
        port,
        allowed_hosts,
    )

    @server.tool()
    def get_unity_balance(member_id: str) -> UnityBalance:
        """Get a Unity by Hard Rock member's points balance, tier and tier credits needed for the next tier."""
        return store.balance(member_id)

    @server.tool()
    def get_points_activity(member_id: str, limit: int = 10) -> PointsActivity:
        """List a Unity member's recent points earned (positive) and redeemed (negative), newest first."""
        return PointsActivity(member_id.strip().upper(), store.activity(member_id, limit))

    return server


def main() -> None:
    """Parse CLI flags and run the server."""
    parser = base_parser(__doc__, DEFAULT_PORT)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("UNITY_DB_PATH", DEFAULT_DB_PATH)))
    args = parser.parse_args()
    create_server(LoyaltyStore(args.db), args.host, args.port, args.allowed_host).run(transport=args.transport)


if __name__ == "__main__":
    main()
