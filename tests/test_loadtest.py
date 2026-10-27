import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from concierge.e2e import Turn
from concierge.loadtest import (
    SCENARIOS,
    Conversation,
    UserResult,
    parse_args,
    play,
    report,
    run,
    stay_dates,
    summarize,
    usernames,
    verify_turn,
)

MEMBER = "HR-300001"
RESULTS = {
    "get_hotel_info": {"check_in_time": "16:00"},
    "list_room_types": {"room_types": [{"code": "DLXK"}]},
    "get_unity_balance": {"points_balance": 3550},
    "get_points_activity": {"activity": [{"points": 2500}]},
    "check_availability": {"check_in": "2026-11-18", "available": [{"room_code": "DLXK"}]},
    "create_booking": {"confirmation_number": "HRM-ABC234", "check_in": "2026-11-18"},
    "get_booking": {"confirmation_number": "HRM-ABC234"},
    "list_bookings": {"bookings": [{"confirmation_number": "HRM-ABC234"}]},
    "cancel_booking": {"confirmation_number": "HRM-ABC234", "status": "cancelled"},
}


def call(name: str, member: str = MEMBER) -> dict[str, Any]:
    return {"name": name, "arguments": {"member_id": member}, "result": json.dumps(RESULTS[name])}


class FakeAgent:
    """Answers each scenario turn with its expected tools; can skip a tool once, misbehave or refuse cancels."""

    def __init__(self, skip_once: str | None = None, member: str = MEMBER, cancel_works: bool = True) -> None:
        self.skip_once = skip_once
        self.member = member
        self.cancel_works = cancel_works
        self.messages: list[tuple[str, str]] = []

    async def __call__(self, message: str, conversation_id: str, token: str | None) -> Turn:
        self.messages.append((conversation_id, message))
        spec = next(
            (t for s in SCENARIOS for t in s.turns if t.prompt.split("{")[0].strip() in message),
            None,
        )
        tools = list(spec.tools) if spec else ["cancel_booking"]
        if self.skip_once and self.skip_once in tools:
            tools.remove(self.skip_once)
            self.skip_once = None
        if "cancel_booking" in tools and not self.cancel_works:
            return Turn(200, "sorry", [], MEMBER, 0.2)
        return Turn(200, "ok", [call(tool, self.member) for tool in tools], MEMBER, 0.5)


def conversation() -> Conversation:
    return Conversation(MEMBER, "2026-11-18", "2026-11-20")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_each_scenario_passes_in_one_conversation(scenario: Any) -> None:
    agent = FakeAgent()
    conv = conversation()
    results, leftover = await play(agent, "tok", scenario, conv)
    assert [r.status for r in results] == ["PASS"] * len(scenario.turns)
    assert leftover is None and conv.confirmation is None
    assert len({cid for cid, _ in agent.messages}) == 1  # multi-turn: one conversation id throughout


async def test_multi_tool_turn_retries_when_a_tool_is_skipped() -> None:
    agent = FakeAgent(skip_once="get_hotel_info")
    results, _ = await play(agent, "tok", SCENARIOS[0], conversation())
    assert (results[0].status, results[0].attempts) == ("PASS", 2)
    assert "(Use the tools: list_bookings, get_unity_balance, get_hotel_info.)" in agent.messages[1][1]


async def test_failed_booking_skips_dependent_turns() -> None:
    results, _ = await play(FakeAgent(skip_once="create_booking"), "tok", SCENARIOS[0], conversation(), retries=0)
    assert [r.status for r in results] == ["PASS", "PASS", "FAIL", "SKIP", "SKIP"]


async def test_booking_left_behind_is_reported() -> None:
    results, leftover = await play(FakeAgent(cancel_works=False), "tok", SCENARIOS[1], conversation(), retries=0)
    assert results[-1].status == "FAIL"
    assert leftover == "HRM-ABC234"


def test_verify_turn_catches_calls_for_another_member() -> None:
    spec = SCENARIOS[1].turns[0]
    turn = Turn(200, "ok", [call("get_unity_balance", "HR-100245"), call("get_points_activity")], MEMBER, 0.1)
    assert verify_turn(spec, turn, conversation()) == "get_unity_balance was called for member HR-100245"


def test_verify_turn_reports_http_and_tool_errors() -> None:
    spec = SCENARIOS[1].turns[0]
    assert verify_turn(spec, Turn(401, "Unauthorized", [], None, 0.0), conversation()).startswith("HTTP 401")
    refused = {"name": "get_unity_balance", "arguments": {}, "result": "Error: not authorized"}
    assert "not authorized" in verify_turn(spec, Turn(200, "x", [refused], MEMBER, 0.1), conversation())


def test_stay_dates_never_overlap_between_users() -> None:
    today = date(2026, 10, 26)
    stays = [stay_dates(i, today) for i in range(1, 51)]
    assert stays[0] == ("2026-11-18", "2026-11-20")
    assert all(stays[i][1] <= stays[i + 1][0] for i in range(len(stays) - 1))
    assert stays[-1][0] <= "2027-10-26"


def test_usernames_from_prefix_or_list() -> None:
    assert usernames(parse_args(["--users", "3"])) == ["loadtest01", "loadtest02", "loadtest03"]
    assert usernames(parse_args(["--usernames", "alex, priya"])) == ["alex", "priya"]


def test_summary_and_report() -> None:
    passing = UserResult("loadtest01", MEMBER, "trip planner", signed_in=True)
    failing = UserResult("loadtest02", "HR-300002", "loyalty review", error="sign-in failed: nope")
    from concierge.loadtest import TurnResult

    passing.turns = [
        TurnResult("overview", "PASS", 4.0, ["list_bookings", "get_unity_balance", "get_hotel_info"], "", 1),
        TurnResult("book", "PASS", 6.0, ["create_booking"], "", 2),
    ]
    summary = summarize([passing, failing], 12.3)
    assert (summary["users"], summary["signed_in"], summary["users_all_passed"]) == (2, 1, 1)
    assert (summary["pass_rate"], summary["retried_turns"], summary["multi_tool_turns"]) == (1.0, 1, 1)
    assert summary["tool_calls"] == 4 and summary["tool_calls_by_tool"]["create_booking"] == 1
    text = report([passing, failing], summary)
    assert "FAIL loadtest02" in text and "sign-in failed: nope" in text
    assert "loadtest01" not in text.split("\n\n")[0]  # passing users only shown with -v
    assert "users signed in      1/2" in text


async def test_run_checks_password_and_memberships(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env_file = ["--env-file", str(tmp_path / "none")]
    assert await run(parse_args(env_file), {}) == 2
    assert "LOADTEST_PASSWORD is not set" in capsys.readouterr().err
    secrets = {"LOADTEST_PASSWORD": "pw", "CONCIERGE_CLIENT_SECRET": "s"}
    assert await run(parse_args([*env_file, "--usernames", "mallory"]), secrets) == 2
    assert "no Unity membership for mallory" in capsys.readouterr().err
    assert await run(parse_args([*env_file, "--concurrency", "0"]), secrets) == 2
