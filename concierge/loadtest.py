"""Scenario load test (`hardrock-concierge-loadtest`): many guests holding multi-turn, multi-tool conversations.

Each user signs in at ThunderID (headless code + PKCE, as in `concierge.e2e`), then holds one conversation with
the agent through the Agent Manager gateway: several turns in the same conversation, mixing questions that need
two or three MCP tools at once with booking a room and cancelling it again. Turns are checked like the e2e check
(expected tools called, no tool errors, every call pinned to the user's own Unity member, tool results), and the
run reports pass rate, tool calls and latency. Users are spread over distinct dates so they never compete for rooms.

    hardrock-concierge-loadtest                          # 50 users loadtest01..50, 5 conversations at a time
    hardrock-concierge-loadtest --users 10 --concurrency 2 -v
    hardrock-concierge-loadtest --usernames alex,priya,daniel --password-env DEMO_PASSWORD
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from concierge.auth import OidcSettings
from concierge.e2e import (
    CONFIRMATION,
    DEFAULT_ENV_FILE,
    DEFAULT_MEMBERS,
    ChatFn,
    LoginError,
    Turn,
    headless_login,
    load_env_file,
    make_chat,
    tool_result,
)
from concierge.identity import MemberMapError, load_members
from concierge.ui_main import DEFAULT_AGENT_URL

DEFAULT_USERS = 50
DEFAULT_CONCURRENCY = 5
DEFAULT_PREFIX = "loadtest"
DEFAULT_TIMEOUT_SECONDS = 300
FIRST_STAY_OFFSET_DAYS = 20
DAYS_BETWEEN_USERS = 3
STAY_NIGHTS = 2


@dataclass
class Conversation:
    """What one user's conversation knows so far."""

    member_id: str
    check_in: str
    check_out: str
    confirmation: str | None = None


Check = Callable[[Turn, Conversation], str | None]


@dataclass(frozen=True)
class TurnSpec:
    """One user message, the MCP tools it must trigger, and what their results must show."""

    label: str
    prompt: str
    tools: tuple[str, ...]
    check: Check | None = None
    needs_booking: bool = False


@dataclass(frozen=True)
class Scenario:
    """A whole conversation, played turn by turn in one conversation id."""

    name: str
    turns: tuple[TurnSpec, ...]


def _check_availability(turn: Turn, conv: Conversation) -> str | None:
    result = tool_result(turn, "check_availability")
    if result.get("check_in") != conv.check_in:
        return f"availability asked for {result.get('check_in')}, not {conv.check_in}"
    return None if result.get("available") else "no rooms available for this user's dates"


def _check_booked(turn: Turn, conv: Conversation) -> str | None:
    booking = tool_result(turn, "create_booking")
    number = str(booking.get("confirmation_number", ""))
    if not CONFIRMATION.fullmatch(number):
        return "create_booking returned no HRM- confirmation number"
    conv.confirmation = number
    if booking.get("check_in") != conv.check_in:
        return f"booked {booking.get('check_in')} instead of {conv.check_in}"
    return None


def _check_listed(turn: Turn, conv: Conversation) -> str | None:
    numbers = [b.get("confirmation_number") for b in tool_result(turn, "list_bookings").get("bookings") or []]
    return None if conv.confirmation in numbers else f"{conv.confirmation} missing from list_bookings"


def _check_details(turn: Turn, conv: Conversation) -> str | None:
    booking = tool_result(turn, "get_booking")
    return None if booking.get("confirmation_number") == conv.confirmation else "get_booking returned another booking"


def _check_cancelled(turn: Turn, conv: Conversation) -> str | None:
    booking = tool_result(turn, "cancel_booking")
    if booking.get("confirmation_number") != conv.confirmation or booking.get("status") != "cancelled":
        return f"cancel_booking returned status={booking.get('status')}"
    conv.confirmation = None
    return None


def _check_balance(turn: Turn, _: Conversation) -> str | None:
    balance = tool_result(turn, "get_unity_balance").get("points_balance")
    return None if isinstance(balance, int) else "get_unity_balance returned no points balance"


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "trip planner",
        (
            TurnSpec(
                "overview (3 tools)",
                "Show my upcoming bookings, my Unity points balance, and the hotel's check-in time.",
                ("list_bookings", "get_unity_balance", "get_hotel_info"),
                _check_balance,
            ),
            TurnSpec(
                "availability",
                "Is a Deluxe King free for 2 guests from {check_in} to {check_out}?",
                ("check_availability",),
                _check_availability,
            ),
            TurnSpec(
                "book (follow-up)",
                "Great, book that Deluxe King (DLXK) for 2 guests from {check_in} to {check_out}.",
                ("create_booking",),
                _check_booked,
            ),
            TurnSpec(
                "list bookings",
                "What are my upcoming bookings now?",
                ("list_bookings",),
                _check_listed,
                needs_booking=True,
            ),
            TurnSpec(
                "cancel",
                "Please cancel booking {confirmation}.",
                ("cancel_booking",),
                _check_cancelled,
                needs_booking=True,
            ),
        ),
    ),
    Scenario(
        "loyalty review",
        (
            TurnSpec(
                "points (2 tools)",
                "Show my Unity points balance and my recent points activity.",
                ("get_unity_balance", "get_points_activity"),
                _check_balance,
            ),
            TurnSpec(
                "rooms + hotel (2 tools)",
                "Which room types do you have, and what time is check-out?",
                ("list_room_types", "get_hotel_info"),
            ),
            TurnSpec(
                "book",
                "Book a Bay View King (BAYK) for 1 guest from {check_in} to {check_out}.",
                ("create_booking",),
                _check_booked,
            ),
            TurnSpec(
                "booking details",
                "Show me the details of booking {confirmation}.",
                ("get_booking",),
                _check_details,
                needs_booking=True,
            ),
            TurnSpec(
                "cancel",
                "Please cancel booking {confirmation}.",
                ("cancel_booking",),
                _check_cancelled,
                needs_booking=True,
            ),
        ),
    ),
)


@dataclass(frozen=True)
class TurnResult:
    """How one turn went. `status` is PASS, FAIL or SKIP."""

    label: str
    status: str
    seconds: float
    tools: list[str]
    detail: str
    attempts: int


@dataclass
class UserResult:
    """Everything one user did."""

    username: str
    member_id: str
    scenario: str
    signed_in: bool = False
    login_seconds: float = 0.0
    error: str = ""
    turns: list[TurnResult] = field(default_factory=list)
    leftover_booking: str | None = None


def stay_dates(index: int, today: date) -> tuple[str, str]:
    """A stay no other user's stay overlaps: user 1 at today+23, user 2 at today+26, ..."""
    check_in = today + timedelta(days=FIRST_STAY_OFFSET_DAYS + DAYS_BETWEEN_USERS * index)
    return check_in.isoformat(), (check_in + timedelta(days=STAY_NIGHTS)).isoformat()


def verify_turn(spec: TurnSpec, turn: Turn, conv: Conversation) -> str | None:
    """None if the turn did what `spec` asks; otherwise why not."""
    if turn.status != 200:
        return f"HTTP {turn.status}: {turn.reply[:160]}"
    if turn.member_id != conv.member_id:
        return f"agent served member {turn.member_id}, expected {conv.member_id}"
    errors = [c for c in turn.tool_calls if str(c.get("result", "")).startswith("Error")]
    if errors:
        return f"{errors[0].get('name')}: {str(errors[0].get('result'))[:160]}"
    foreign = [c for c in turn.tool_calls if c.get("arguments", {}).get("member_id") not in (None, conv.member_id)]
    if foreign:
        return f"{foreign[0].get('name')} was called for member {foreign[0]['arguments']['member_id']}"
    missing = [tool for tool in spec.tools if not turn.calls(tool)]
    if missing:
        used = ", ".join(c.get("name", "?") for c in turn.tool_calls) or "no tools"
        return f"missing {', '.join(missing)} (used {used})"
    return spec.check(turn, conv) if spec.check else None


async def play(
    chat: ChatFn,
    token: str,
    scenario: Scenario,
    conv: Conversation,
    retries: int = 1,
    on_turn: Callable[[TurnSpec, Turn], None] | None = None,
) -> tuple[list[TurnResult], str | None]:
    """Play `scenario` in one conversation; cancel a booking left behind. Returns turn results and any leftover."""
    conversation_id = uuid.uuid4().hex
    results = []
    for spec in scenario.turns:
        if spec.needs_booking and not conv.confirmation:
            results.append(TurnResult(spec.label, "SKIP", 0.0, [], "no booking to work with", 0))
            continue
        prompt = spec.prompt.format(check_in=conv.check_in, check_out=conv.check_out, confirmation=conv.confirmation)
        result = TurnResult(spec.label, "FAIL", 0.0, [], "not run", 0)
        for attempt in range(1, retries + 2):
            message = prompt if attempt == 1 else f"{prompt} (Use the tools: {', '.join(spec.tools)}.)"
            turn = await chat(message, conversation_id, token)
            if on_turn:
                on_turn(spec, turn)
            failure = verify_turn(spec, turn, conv)
            tools = [c.get("name", "?") for c in turn.tool_calls]
            result = TurnResult(spec.label, "FAIL" if failure else "PASS", turn.seconds, tools, failure or "", attempt)
            if not failure or turn.status != 200 or all(turn.calls(t) for t in spec.tools):
                break  # retry only when the model skipped a tool
        results.append(result)
    return results, await _clean_up(chat, token, conversation_id, conv)


async def _clean_up(chat: ChatFn, token: str, conversation_id: str, conv: Conversation) -> str | None:
    for _ in range(2):
        if not conv.confirmation:
            return None
        turn = await chat(f"Please cancel booking {conv.confirmation}.", conversation_id, token)
        if turn.status == 200 and tool_result(turn, "cancel_booking").get("status") == "cancelled":
            conv.confirmation = None
    return conv.confirmation


async def run_user(
    index: int,
    username: str,
    member_id: str,
    password: str,
    settings: OidcSettings,
    idp: httpx.AsyncClient,
    chat: ChatFn,
    today: date,
    retries: int,
    on_turn: Callable[[str, TurnSpec, Turn], None] | None = None,
) -> UserResult:
    """Sign one user in and play their scenario (scenarios alternate by user number)."""
    scenario = SCENARIOS[(index - 1) % len(SCENARIOS)]
    result = UserResult(username, member_id, scenario.name)
    started = time.monotonic()
    try:
        token = await headless_login(idp, settings, username, password)
    except (LoginError, httpx.HTTPError) as exc:
        result.error = f"sign-in failed: {exc}"
        return result
    result.signed_in, result.login_seconds = True, time.monotonic() - started
    check_in, check_out = stay_dates(index, today)
    conv = Conversation(member_id, check_in, check_out)
    try:
        result.turns, result.leftover_booking = await play(
            chat,
            token,
            scenario,
            conv,
            retries,
            (lambda spec, turn: on_turn(username, spec, turn)) if on_turn else None,
        )
    except httpx.HTTPError as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.leftover_booking = conv.confirmation
    return result


def summarize(results: Sequence[UserResult], wall_seconds: float) -> dict[str, Any]:
    """Aggregate numbers for the report and the JSON output."""
    turns = [t for r in results for t in r.turns]
    timed = sorted(t.seconds for t in turns if t.status != "SKIP")
    checked = [t for t in turns if t.status != "SKIP"]
    tool_counts = Counter(tool for t in turns for tool in t.tools)
    return {
        "users": len(results),
        "signed_in": sum(r.signed_in for r in results),
        "users_all_passed": sum(
            r.signed_in and not r.error and all(t.status == "PASS" for t in r.turns) for r in results
        ),
        "turns_passed": sum(t.status == "PASS" for t in turns),
        "turns_failed": sum(t.status == "FAIL" for t in turns),
        "turns_skipped": sum(t.status == "SKIP" for t in turns),
        "pass_rate": round(sum(t.status == "PASS" for t in checked) / len(checked), 3) if checked else 0.0,
        "retried_turns": sum(t.attempts > 1 for t in turns),
        "tool_calls": sum(tool_counts.values()),
        "tool_calls_by_tool": dict(sorted(tool_counts.items())),
        "multi_tool_turns": sum(len(set(t.tools)) >= 2 and t.status == "PASS" for t in turns),
        "turn_latency_p50": round(statistics.median(timed), 1) if timed else 0.0,
        "turn_latency_p95": round(timed[max(0, round(0.95 * len(timed)) - 1)], 1) if timed else 0.0,
        "turn_latency_max": round(timed[-1], 1) if timed else 0.0,
        "leftover_bookings": [r.leftover_booking for r in results if r.leftover_booking],
        "wall_seconds": round(wall_seconds, 1),
    }


def report(results: Sequence[UserResult], summary: Mapping[str, Any], verbose: bool = False) -> str:
    """Per-user lines (only failures unless verbose), then the totals."""
    lines = []
    for r in results:
        failed = [t for t in r.turns if t.status == "FAIL"]
        ok = r.signed_in and not r.error and not failed
        if verbose or not ok:
            marks = " ".join({"PASS": "✓", "FAIL": "✗", "SKIP": "·"}[t.status] for t in r.turns)
            lines.append(f"{'PASS' if ok else 'FAIL'} {r.username:<12} {r.member_id} {r.scenario:<15} {marks}")
            if r.error:
                lines.append(f"     {r.error}")
            lines.extend(f"     ✗ {t.label}: {t.detail}" for t in failed)
            if r.leftover_booking:
                lines.append(f"     booking {r.leftover_booking} could not be cancelled")
    s = summary
    lines += [
        "",
        f"users signed in      {s['signed_in']}/{s['users']}   all turns passed: {s['users_all_passed']}/{s['users']}",
        f"turns                {s['turns_passed']} passed, {s['turns_failed']} failed, {s['turns_skipped']} skipped"
        f"   pass rate {s['pass_rate']:.0%}   retried {s['retried_turns']}",
        f"MCP tool calls       {s['tool_calls']} ({s['multi_tool_turns']} turns used 2+ tools)",
        "                     " + ", ".join(f"{k} {v}" for k, v in s["tool_calls_by_tool"].items()),
        f"turn latency         p50 {s['turn_latency_p50']}s  p95 {s['turn_latency_p95']}s"
        f"  max {s['turn_latency_max']}s",
        f"wall time            {s['wall_seconds']}s",
        f"leftover bookings    {', '.join(s['leftover_bookings']) or 'none'}",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Flags; settings fall back to the environment, then the env file."""
    parser = argparse.ArgumentParser(
        prog="hardrock-concierge-loadtest",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--users", type=int, default=DEFAULT_USERS, help="how many prefixNN users (default %(default)s)"
    )
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="username prefix (default %(default)s)")
    parser.add_argument("--usernames", help="comma-separated users instead of prefix01..NN, e.g. alex,priya,daniel")
    parser.add_argument("--password-env", default="LOADTEST_PASSWORD", help="variable holding their password")
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="conversations at once (default %(default)s)"
    )
    parser.add_argument("--retries", type=int, default=1, help="extra attempts when the model skips a tool")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="seconds per agent turn")
    parser.add_argument(
        "--min-pass-rate", type=float, default=0.9, help="exit 1 below this turn pass rate (default 0.9)"
    )
    parser.add_argument("--agent-url", default=None, help=f"default AGENT_URL or {DEFAULT_AGENT_URL}")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--members", type=Path, default=DEFAULT_MEMBERS)
    parser.add_argument("--json", type=Path, help="also write the full results here")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every user and every turn as it finishes")
    return parser.parse_args(argv)


def usernames(args: argparse.Namespace) -> list[str]:
    """The users to run: --usernames, or prefix01..prefixNN."""
    if args.usernames:
        return [name.strip() for name in args.usernames.split(",") if name.strip()]
    return [f"{args.prefix}{i:02d}" for i in range(1, args.users + 1)]


async def run(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    """Run every user's scenario with at most --concurrency in flight. Returns 0 if the run met the bar."""
    env = {**load_env_file(args.env_file), **environ}
    password = env.get(args.password_env, "")
    if not password:
        print(f"error: {args.password_env} is not set (environment or {args.env_file})", file=sys.stderr)
        return 2
    if args.concurrency < 1:
        print("error: --concurrency must be at least 1", file=sys.stderr)
        return 2
    try:
        settings = OidcSettings.from_env(env)
        members = load_members(args.members)
    except (ValueError, MemberMapError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    names = usernames(args)
    unmapped = [name for name in names if name.lower() not in members]
    if unmapped:
        print(f"error: no Unity membership for {', '.join(unmapped[:5])} in {args.members}", file=sys.stderr)
        return 2
    agent_url = (args.agent_url or env.get("AGENT_URL") or DEFAULT_AGENT_URL).rstrip("/") + "/"
    today = date.today()

    def show(username: str, spec: TurnSpec, turn: Turn) -> None:
        if args.verbose:
            tools = ", ".join(c.get("name", "?") for c in turn.tool_calls) or "-"
            print(f"  {username:<12} {spec.label:<24} {turn.seconds:5.1f}s  {tools}", flush=True)

    print(f"{len(names)} users, {args.concurrency} at a time, against {agent_url}", flush=True)
    gate = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(max_connections=args.concurrency * 2)
    async with (
        httpx.AsyncClient(timeout=30, limits=limits) as idp,
        httpx.AsyncClient(base_url=agent_url, limits=limits) as agent,
    ):
        chat = make_chat(agent, timeout=args.timeout)

        async def one(index: int, name: str) -> UserResult:
            async with gate:
                result = await run_user(
                    index, name, members[name.lower()], password, settings, idp, chat, today, args.retries, show
                )
                done = "PASS" if result.signed_in and all(t.status == "PASS" for t in result.turns) else "FAIL"
                print(f"{done} {name} ({result.scenario})", flush=True)
                return result

        started = time.monotonic()
        results = await asyncio.gather(*(one(i, name) for i, name in enumerate(names, start=1)))
        summary = summarize(results, time.monotonic() - started)

    print("\n" + report(results, summary, args.verbose))
    if args.json:
        args.json.write_text(
            json.dumps({"summary": summary, "users": [asdict(r) for r in results]}, indent=2), encoding="utf-8"
        )
        print(f"results written to {args.json}")
    met = (
        summary["signed_in"] == summary["users"]
        and summary["pass_rate"] >= args.min_pass_rate
        and not summary["leftover_bookings"]
    )
    return 0 if met else 1


def main() -> None:
    """Entry point for `hardrock-concierge-loadtest`."""
    sys.exit(asyncio.run(run(parse_args(), os.environ)))


if __name__ == "__main__":
    main()
