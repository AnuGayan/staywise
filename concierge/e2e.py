"""End-to-end check of the deployed concierge (`hardrock-concierge-e2e`).

Signs a demo guest in at ThunderID without a browser (authorization code + PKCE, driving ThunderID's
login flow API the way the sign-in page does), then chats with the agent through the Agent Manager
gateway with that token, the way the UI does. Each step checks which tools the agent called, that
every call was pinned to the signed-in guest's Unity member id, and what the tools returned. The
booking it makes is cancelled again, so demo data is left as it was.

    hardrock-concierge-e2e                      # reads .env.thunder; signs in as alex
    hardrock-concierge-e2e --user priya -v      # another guest; print every reply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from concierge.auth import OidcSettings, code_challenge
from concierge.identity import load_members
from concierge.ui_main import DEFAULT_AGENT_URL

DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env.thunder"
DEFAULT_MEMBERS = Path(__file__).resolve().parent.parent / "config" / "members.json"
CHAT_TIMEOUT_SECONDS = 180
MAX_FLOW_STEPS = 6
CONFIRMATION = re.compile(r"HRM-[A-Z0-9]{6}")


class LoginError(Exception):
    """The headless sign-in did not produce a user token."""


def load_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE pairs from an env file; tolerates `KEY = 'value'` and comments. Missing file -> {}."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if match and not line.lstrip().startswith("#"):
            values[match.group(1)] = match.group(2).strip("'\"")
    return values


async def headless_login(http: httpx.AsyncClient, settings: OidcSettings, username: str, password: str) -> str:
    """Sign `username` in like the browser would and return their access token.

    authorize -> ThunderID login flow (/flow/execute) -> /oauth2/auth/callback -> code -> /oauth2/token.

    Raises:
        LoginError: if any step is refused or answers unexpectedly.
    """
    verifier, state, nonce = (secrets.token_urlsafe(32) for _ in range(3))
    query = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": settings.redirect_uri,
        "scope": " ".join(settings.scopes),
        "resource": settings.resource,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
        "prompt": "login",
    }
    authorize = await http.get(f"{settings.issuer}/oauth2/authorize?{urlencode(query)}", follow_redirects=False)
    gate = _query(authorize.headers.get("location", ""))
    if "error" in gate:
        raise LoginError(
            f"authorize refused: {gate['error']} {gate.get('errorMessage', gate.get('error_description', ''))}"
        )
    if "executionId" not in gate or "authId" not in gate:
        raise LoginError(f"authorize answered HTTP {authorize.status_code} without a sign-in redirect")

    assertion = await _run_login_flow(http, settings.issuer, gate["executionId"], username, password)
    callback = await http.post(
        f"{settings.issuer}/oauth2/auth/callback", json={"authId": gate["authId"], "assertion": assertion}
    )
    redirect = _json(callback).get("redirect_uri", "")
    params = _query(redirect)
    if callback.status_code != httpx.codes.OK or "code" not in params:
        raise LoginError(f"auth callback answered HTTP {callback.status_code}: {callback.text[:200]}")
    if params.get("state") != state:
        raise LoginError("auth callback returned a different state")

    token = await http.post(
        f"{settings.issuer}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": params["code"],
            "redirect_uri": settings.redirect_uri,
            "code_verifier": verifier,
            "resource": settings.resource,
        },
        auth=(settings.client_id, settings.client_secret),
    )
    body = _json(token)
    if token.status_code != httpx.codes.OK or not isinstance(body.get("access_token"), str):
        raise LoginError(f"token request answered HTTP {token.status_code}: {body.get('error_description', body)}")
    return body["access_token"]


async def _run_login_flow(http: httpx.AsyncClient, issuer: str, execution_id: str, username: str, password: str) -> str:
    answers = {"username": username, "password": password}
    step = _json(await http.post(f"{issuer}/flow/execute", json={"executionId": execution_id}))
    for _ in range(MAX_FLOW_STEPS):
        status = step.get("flowStatus")
        if status == "COMPLETE" and step.get("assertion"):
            return str(step["assertion"])
        if status != "INCOMPLETE":
            error = step.get("error") or step
            raise LoginError(f"sign-in flow failed: {json.dumps(error)[:200]}")
        data = step.get("data") or {}
        wanted = [field_spec.get("identifier") for field_spec in data.get("inputs") or []]
        unknown = [name for name in wanted if name not in answers]
        if unknown:
            raise LoginError(f"sign-in flow asks for {unknown}, which this check can't answer")
        request: dict[str, Any] = {
            "executionId": step.get("executionId", execution_id),
            "challengeToken": step.get("challengeToken"),
            "inputs": {name: answers[name] for name in wanted},
        }
        actions = data.get("actions") or []
        if actions:
            request["action"] = actions[0].get("ref")
        step = _json(await http.post(f"{issuer}/flow/execute", json=request))
    raise LoginError(f"sign-in flow did not complete within {MAX_FLOW_STEPS} steps")


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


@dataclass
class Turn:
    """One chat exchange as the agent reported it."""

    status: int
    reply: str
    tool_calls: list[dict[str, Any]]
    member_id: str | None
    seconds: float

    def calls(self, tool: str) -> list[dict[str, Any]]:
        """The calls this turn made to `tool`."""
        return [call for call in self.tool_calls if call.get("name") == tool]


@dataclass
class Context:
    """What the scenario learns as it goes (dates it books, the confirmation number it gets back)."""

    member_id: str
    check_in: str
    check_out: str
    confirmation: str | None = None
    notes: dict[str, str] = field(default_factory=dict)


Check = Callable[[Turn, Context], str | None]


@dataclass(frozen=True)
class Step:
    """A user message, the tool it must trigger, and what the tool's result must show."""

    name: str
    prompt: str
    tool: str
    check: Check | None = None
    needs_confirmation: bool = False


def tool_result(turn: Turn, tool: str) -> dict[str, Any]:
    """The last successful call's result for `tool`, parsed from JSON ({} if none or not JSON)."""
    for call in reversed(turn.calls(tool)):
        try:
            parsed = json.loads(call.get("result", ""))
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _check_hotel(turn: Turn, _: Context) -> str | None:
    info = tool_result(turn, "get_hotel_info")
    return None if info.get("check_in_time") else "get_hotel_info returned no check-in time"


def _check_rooms(turn: Turn, _: Context) -> str | None:
    rooms = tool_result(turn, "list_room_types").get("room_types") or []
    return None if any(room.get("code") == "DLXK" for room in rooms) else "list_room_types has no DLXK"


def _check_availability(turn: Turn, ctx: Context) -> str | None:
    result = tool_result(turn, "check_availability")
    if result.get("check_in") != ctx.check_in:
        return f"check_availability was asked about {result.get('check_in')}, not {ctx.check_in}"
    return None if result.get("available") else "no rooms available for the test dates"


def _check_create(turn: Turn, ctx: Context) -> str | None:
    booking = tool_result(turn, "create_booking")
    number = str(booking.get("confirmation_number", ""))
    if not CONFIRMATION.fullmatch(number):
        return "create_booking returned no HRM- confirmation number"
    if booking.get("check_in") != ctx.check_in or booking.get("status") != "upcoming":
        return f"booking has check_in={booking.get('check_in')} status={booking.get('status')}"
    ctx.confirmation = number
    return None


def _check_get(turn: Turn, ctx: Context) -> str | None:
    booking = tool_result(turn, "get_booking")
    return None if booking.get("confirmation_number") == ctx.confirmation else "get_booking returned another booking"


def _check_list(turn: Turn, ctx: Context) -> str | None:
    numbers = [b.get("confirmation_number") for b in tool_result(turn, "list_bookings").get("bookings") or []]
    return None if ctx.confirmation in numbers else f"{ctx.confirmation} missing from list_bookings"


def _check_cancel(turn: Turn, ctx: Context) -> str | None:
    booking = tool_result(turn, "cancel_booking")
    if booking.get("confirmation_number") != ctx.confirmation or booking.get("status") != "cancelled":
        return f"cancel_booking returned status={booking.get('status')}"
    ctx.confirmation = None  # cleaned up
    return None


def _check_balance(turn: Turn, ctx: Context) -> str | None:
    balance = tool_result(turn, "get_unity_balance")
    if not isinstance(balance.get("points_balance"), int):
        return "get_unity_balance returned no points balance"
    ctx.notes["points"] = f"{balance['points_balance']:,} points, {balance.get('tier')}"
    return None


def _check_activity(turn: Turn, _: Context) -> str | None:
    return None if tool_result(turn, "get_points_activity").get("activity") else "no points activity returned"


def scenario() -> list[Step]:
    """The guest's conversation: every tool on all three MCP servers, in an order that cleans up after itself."""
    return [
        Step("hotel info", "What time is check-in and check-out at the hotel?", "get_hotel_info", _check_hotel),
        Step("room types", "What room types do you have?", "list_room_types", _check_rooms),
        Step(
            "availability",
            "Is a Deluxe King free for 2 guests from {check_in} to {check_out}?",
            "check_availability",
            _check_availability,
        ),
        Step(
            "book a room",
            "Book a Deluxe King (room code DLXK) for 2 guests from {check_in} to {check_out}.",
            "create_booking",
            _check_create,
        ),
        Step(
            "get booking",
            "Show me the details of booking {confirmation}.",
            "get_booking",
            _check_get,
            needs_confirmation=True,
        ),
        Step("list bookings", "What are my upcoming bookings?", "list_bookings", _check_list, needs_confirmation=True),
        Step(
            "cancel booking",
            "Please cancel booking {confirmation}.",
            "cancel_booking",
            _check_cancel,
            needs_confirmation=True,
        ),
        Step("points balance", "What's my Unity points balance?", "get_unity_balance", _check_balance),
        Step("points activity", "Show my recent Unity points activity.", "get_points_activity", _check_activity),
    ]


@dataclass(frozen=True)
class Outcome:
    """How one check ended. `status` is PASS, FAIL or SKIP."""

    name: str
    status: str
    detail: str
    seconds: float = 0.0
    attempts: int = 0


ChatFn = Callable[[str, str, str | None], Awaitable[Turn]]


def make_chat(agent: httpx.AsyncClient, timeout: float = CHAT_TIMEOUT_SECONDS) -> ChatFn:
    """POST /chat to the agent (through the gateway) as the holder of `token`."""

    async def chat(message: str, conversation_id: str, token: str | None) -> Turn:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        started = time.monotonic()
        response = await agent.post(
            "chat",
            json={"message": message, "conversation_id": conversation_id},
            headers=headers,
            timeout=timeout,
        )
        body = _json(response)
        return Turn(
            status=response.status_code,
            reply=str(body.get("reply") or body.get("error") or body.get("message") or response.text[:200]),
            tool_calls=list(body.get("tool_calls") or []),
            member_id=body.get("member_id"),
            seconds=time.monotonic() - started,
        )

    return chat


async def check_gateway_rejects(chat: ChatFn) -> list[Outcome]:
    """Requests without a valid user token must never reach the agent's tools."""
    outcomes = []
    for name, token in (("no token is rejected", None), ("forged token is rejected", "aaa.bbb.ccc")):
        turn = await chat("What's my Unity points balance?", uuid.uuid4().hex, token)
        ok = turn.status in (401, 403) and not turn.tool_calls
        outcomes.append(Outcome(name, "PASS" if ok else "FAIL", f"HTTP {turn.status}", turn.seconds))
    return outcomes


async def run_scenario(
    chat: ChatFn,
    token: str,
    ctx: Context,
    retries: int = 1,
    on_turn: Callable[[Step, Turn], None] | None = None,
) -> list[Outcome]:
    """Play the guest's conversation and check each step. A step whose tool wasn't called is retried in a fresh
    conversation with a more explicit request, because small models sometimes answer without the tool."""
    outcomes = []
    for step in scenario():
        if step.needs_confirmation and not ctx.confirmation:
            outcomes.append(Outcome(step.name, "SKIP", "no booking to work with (booking step failed)"))
            continue
        prompt = step.prompt.format(check_in=ctx.check_in, check_out=ctx.check_out, confirmation=ctx.confirmation)
        outcome = Outcome(step.name, "FAIL", "not run")
        for attempt in range(1, retries + 2):
            message = prompt if attempt == 1 else f"{prompt} Use the {step.tool} tool."
            turn = await chat(message, uuid.uuid4().hex, token)
            if on_turn:
                on_turn(step, turn)
            failure = _verify(step, turn, ctx)
            outcome = Outcome(
                step.name, "FAIL" if failure else "PASS", failure or _summary(step, turn, ctx), turn.seconds, attempt
            )
            if not failure or turn.status != 200 or turn.calls(step.tool):
                break  # only "the model skipped the tool" is worth a retry
        outcomes.append(outcome)
    return outcomes


def _verify(step: Step, turn: Turn, ctx: Context) -> str | None:
    if turn.status != 200:
        return f"HTTP {turn.status}: {turn.reply}"
    if turn.member_id != ctx.member_id:
        return f"agent served member {turn.member_id}, expected {ctx.member_id}"
    errors = [c for c in turn.tool_calls if str(c.get("result", "")).startswith("Error")]
    if errors:
        return f"{errors[0]['name']}: {str(errors[0]['result'])[:160]}"
    foreign = [c for c in turn.tool_calls if c.get("arguments", {}).get("member_id") not in (None, ctx.member_id)]
    if foreign:
        return f"{foreign[0]['name']} was called for member {foreign[0]['arguments']['member_id']}"
    if not turn.calls(step.tool):
        called = ", ".join(c.get("name", "?") for c in turn.tool_calls) or "no tools"
        return f"expected {step.tool}, the model used {called}"
    return step.check(turn, ctx) if step.check else None


def _summary(step: Step, turn: Turn, ctx: Context) -> str:
    if step.tool == "create_booking":
        return f"booked {ctx.confirmation}"
    if step.tool == "get_unity_balance":
        return ctx.notes.get("points", "")
    return ", ".join(c["name"] for c in turn.tool_calls)


def report(outcomes: list[Outcome]) -> str:
    """A fixed-width results table plus a one-line verdict."""
    lines = [f"{'':4} {'check':<26} {'time':>7}  detail"]
    for outcome in outcomes:
        retry = f" (attempt {outcome.attempts})" if outcome.attempts > 1 else ""
        lines.append(f"{outcome.status:<4} {outcome.name:<26} {outcome.seconds:>6.1f}s  {outcome.detail}{retry}")
    failed = sum(o.status == "FAIL" for o in outcomes)
    skipped = sum(o.status == "SKIP" for o in outcomes)
    passed = len(outcomes) - failed - skipped
    lines.append(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Flags; settings fall back to the environment, then to the env file."""
    parser = argparse.ArgumentParser(prog="hardrock-concierge-e2e", description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", default="alex", help="demo guest to sign in as (default %(default)s)")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="where thunder-setup.sh wrote secrets")
    parser.add_argument("--agent-url", default=None, help=f"default AGENT_URL or {DEFAULT_AGENT_URL}")
    parser.add_argument("--members", type=Path, default=DEFAULT_MEMBERS)
    parser.add_argument("--retries", type=int, default=1, help="extra attempts when the model skips a tool")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every reply and tool call")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    """Sign in, run the checks, print the report. Returns 0 if everything passed."""
    env = {**load_env_file(args.env_file), **environ}
    password = env.get("DEMO_PASSWORD", "")
    if not password:
        print(f"error: DEMO_PASSWORD is not set (looked in the environment and {args.env_file})", file=sys.stderr)
        return 2
    try:
        settings = OidcSettings.from_env(env)
        member_id = load_members(args.members)[args.user.lower()]
    except KeyError:
        print(f"error: {args.user} has no Unity membership in {args.members}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    agent_url = (args.agent_url or env.get("AGENT_URL") or DEFAULT_AGENT_URL).rstrip("/") + "/"
    today = date.today()
    ctx = Context(
        member_id=member_id,
        check_in=(today + timedelta(days=45)).isoformat(),
        check_out=(today + timedelta(days=47)).isoformat(),
    )

    def show(step: Step, turn: Turn) -> None:
        if args.verbose:
            calls = "; ".join(f"{c['name']}({json.dumps(c.get('arguments', {}))})" for c in turn.tool_calls)
            print(f"  [{step.name}] tools: {calls or '-'}\n  [{step.name}] reply: {turn.reply[:300]}", flush=True)

    async with httpx.AsyncClient(timeout=30) as idp, httpx.AsyncClient(base_url=agent_url) as agent:
        print(f"Signing in as {args.user} at {settings.issuer} ...", flush=True)
        started = time.monotonic()
        try:
            token = await headless_login(idp, settings, args.user, password)
        except (LoginError, httpx.HTTPError) as exc:
            print(report([Outcome("sign in", "FAIL", str(exc), time.monotonic() - started)]))
            return 1
        outcomes = [Outcome("sign in (code + PKCE)", "PASS", f"user token for {args.user}", time.monotonic() - started)]
        print(f"Chatting with {agent_url} as member {member_id} ...", flush=True)
        chat = make_chat(agent)
        try:
            outcomes += await check_gateway_rejects(chat)
            outcomes += await run_scenario(chat, token, ctx, args.retries, show)
        except httpx.HTTPError as exc:
            outcomes.append(Outcome("agent reachable", "FAIL", f"{type(exc).__name__}: {exc}"))
        if ctx.confirmation:
            outcomes.append(Outcome("clean-up", "FAIL", f"booking {ctx.confirmation} was left in place; cancel it"))
    print(report(outcomes))
    return 0 if all(o.status != "FAIL" for o in outcomes) else 1


def main() -> None:
    """Entry point for `hardrock-concierge-e2e`."""
    sys.exit(asyncio.run(run(parse_args(), os.environ)))


if __name__ == "__main__":
    main()
