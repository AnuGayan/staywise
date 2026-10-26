"""The demo script end to end, in memory: real guarded MCP servers, real transport/broker/coordinator,
a fake Thunder with 1.0.0's exchange semantics, and a scripted model."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from servers.runner import GuardSettings
from servers.store import HotelStore
from staywise.auth.broker import UserGrant
from staywise.events import Event
from staywise.session import ChatSession, Guest, SessionDeps
from tests import fakes
from tests.conftest import AGENT_ID, GUEST, STEPUP_CLIENT

DATES = {"check_in": "2026-11-14", "check_out": "2026-11-16"}


class Harness:
    def __init__(self, session: ChatSession, llm: fakes.ScriptedLLM, thunder: fakes.FakeThunder) -> None:
        self.session = session
        self.llm = llm
        self.thunder = thunder
        self.queue = session.bus.subscribe()
        self.events: list[Event] = []

    async def turn(self, message: str) -> list[Event]:
        """Run one turn and return its events; step-ups are left for the test to answer."""
        self.session.start_turn(message)
        turn_events: list[Event] = []
        while True:
            event = await asyncio.wait_for(self.queue.get(), timeout=5)
            turn_events.append(event)
            self.events.append(event)
            if event.type in ("turn_finished", "turn_failed"):
                return turn_events

    async def next_event(self, type_: str) -> Event:
        while True:
            event = await asyncio.wait_for(self.queue.get(), timeout=5)
            self.events.append(event)
            if event.type == type_:
                return event

    async def drain_turn(self) -> list[Event]:
        drained: list[Event] = []
        while True:
            event = await asyncio.wait_for(self.queue.get(), timeout=5)
            drained.append(event)
            self.events.append(event)
            if event.type in ("turn_finished", "turn_failed"):
                return drained


def of(events: list[Event], type_: str) -> list[dict[str, Any]]:
    return [e.data for e in events if e.type == type_]


@dataclass
class Env:
    tmp_path: Path
    store: HotelStore
    guard_settings: GuardSettings

    @asynccontextmanager
    async def harness(self, script: list[Any], stepup_timeout: float = 5) -> AsyncIterator[Harness]:
        """Servers, session and fakes for one test; opened and closed in the test's own task (anyio needs that)."""
        async with fakes.mcp_servers(self.store, self.guard_settings) as router:
            policy = fakes.policy()
            policy = replace(policy, user_stepup=replace(policy.user_stepup, timeout_seconds=stepup_timeout))
            thunder, llm = fakes.FakeThunder(), fakes.ScriptedLLM(script)
            deps = SessionDeps(
                settings=fakes.settings(self.tmp_path),
                thunder=thunder,
                policy=policy,
                llm=llm,
                model="test",
                mcp_configs=fakes.server_configs(),
                inner_transport=router,
            )
            grant = UserGrant(GUEST, fakes.token_set(fakes.LOGIN_SCOPES))
            session = ChatSession("s1", Guest(GUEST, "Demo Guest"), grant, deps)
            try:
                yield Harness(session, llm, thunder)
            finally:
                await session.close()


@pytest.fixture
def env(tmp_path: Path, store: HotelStore, guard_settings: GuardSettings) -> Env:
    return Env(tmp_path, store, guard_settings)


async def book(h: Harness) -> str:
    events = await h.turn("book it")
    created = of(events, "tool_call_finished")[0]
    assert created["ok"], created
    return created["resultPreview"].split('"id": "')[1].split('"')[0]


async def test_search_uses_baseline_token(env: Env) -> None:
    async with env.harness([[("search_hotels", {"city": "Galle", "sea_view": True})], "Two options."]) as h:
        events = await h.turn("sea view in Galle")

        issued = of(events, "token_issued")
        assert of(events, "tool_call_finished")[0]["ok"]
        assert [i["strategy"] for i in issued] == ["cached"]  # minted at connect, reused for the call
        assert issued[0]["scopes"] == ["hotel:read"]
        assert issued[0]["claims"]["act"] == {"sub": AGENT_ID}
        assert of(events, "assistant_message")[0]["content"] == "Two options."


async def test_booking_steps_up_automatically_by_token_exchange(env: Env) -> None:
    async with env.harness([[("create_reservation", {"room_id": "lighthouse-deluxe", **DATES})], "Booked."]) as h:
        events = await h.turn("book the deluxe")

        finished = of(events, "tool_call_finished")[0]
        issued = of(events, "token_issued")[0]
        assert finished["ok"], finished
        assert issued["strategy"] == "auto-obo"
        assert issued["scopes"] == ["booking:read", "booking:write"]
        assert issued["claims"]["sub"] == GUEST and issued["claims"]["act"] == {"sub": AGENT_ID}
        assert ("token_exchange", frozenset({"booking:read", "booking:write"})) in h.thunder.calls
        assert not of(events, "stepup_required")


async def test_front_desk_uses_agent_identity(env: Env) -> None:
    async with env.harness([[("notify_front_desk", {"message": "Arriving 11pm"})], "Done."]) as h:
        events = await h.turn("tell the front desk")

        issued = of(events, "token_issued")[0]
        assert of(events, "tool_call_finished")[0]["ok"]
        assert issued["claims"]["sub"] == AGENT_ID and "act" not in issued["claims"]
        assert issued["scopes"] == ["ops:notify"]


async def test_large_charge_waits_for_mfa_then_resumes(env: Env) -> None:
    async with env.harness(
        [
            [("create_reservation", {"room_id": "ocean-suite", **DATES})],
            "Booked.",
            [("charge_payment", {"reservation_id": "PLACEHOLDER", "amount": 1240, "purpose": "prepayment"})],
            "Paid.",
        ]
    ) as h:
        reservation_id = await book(h)
        h.llm._script[0][0][1]["reservation_id"] = reservation_id

        h.session.start_turn("prepay it")
        required = (await h.next_event("stepup_required")).data
        assert required["scope"] == ["payment:highvalue"]
        assert "over $500" in required["reason"]
        assert required["startUrl"] == f"/auth/stepup/start/{required['stepupId']}"

        stepup = h.session.stepups.get(required["stepupId"])
        assert stepup is not None
        h.session.stepups.approve(stepup, fakes.token_set("openid payment:highvalue", client_id=STEPUP_CLIENT))
        rest = await h.drain_turn()

        assert of(rest, "stepup_resolved")[0]["outcome"] == "approved"
        finished = of(rest, "tool_call_finished")[0]
        assert finished["ok"], finished
        issued = of(rest, "token_issued")[0]
        assert issued["strategy"] == "user-mfa" and issued["claims"]["client_id"] == STEPUP_CLIENT
        assert '"authorized_client": "staywise-stepup"' in finished["resultPreview"]


async def test_declined_mfa_becomes_a_tool_error_and_the_connection_survives(env: Env) -> None:
    async with env.harness(
        [
            [("create_reservation", {"room_id": "ocean-suite", **DATES})],
            "Booked.",
            [("charge_payment", {"reservation_id": "PLACEHOLDER", "amount": 1240})],
            "Okay, not charged.",
            [("get_loyalty_balance", {})],
            "12,500 points.",
        ]
    ) as h:
        reservation_id = await book(h)
        h.llm._script[0][0][1]["reservation_id"] = reservation_id

        h.session.start_turn("prepay it")
        required = (await h.next_event("stepup_required")).data
        stepup = h.session.stepups.get(required["stepupId"])
        assert stepup is not None
        h.session.stepups.decline(stepup)
        rest = await h.drain_turn()

        finished = of(rest, "tool_call_finished")[0]
        assert of(rest, "stepup_resolved")[0]["outcome"] == "declined"
        assert not finished["ok"] and "declined" in finished["error"]
        assert "declined" in h.llm.tool_results()[-1]

        after = await h.turn("points?")
        assert of(after, "tool_call_finished")[0]["ok"]  # the payments MCP connection is still usable


async def test_mfa_timeout_is_reported(env: Env) -> None:
    async with env.harness(
        [
            [("create_reservation", {"room_id": "ocean-suite", **DATES})],
            "Booked.",
            [("charge_payment", {"reservation_id": "PLACEHOLDER", "amount": 1240})],
            "Timed out.",
        ],
        stepup_timeout=0.2,
    ) as h:
        reservation_id = await book(h)
        h.llm._script[0][0][1]["reservation_id"] = reservation_id
        events = await h.turn("prepay it")

        assert of(events, "stepup_resolved")[0]["outcome"] == "timeout"
        assert "in time" in of(events, "tool_call_finished")[0]["error"]


async def test_mfa_token_is_reused_while_valid(env: Env) -> None:
    async with env.harness(
        [
            [("create_reservation", {"room_id": "ocean-suite", **DATES})],
            "Booked.",
            [("charge_payment", {"reservation_id": "PLACEHOLDER", "amount": 600})],
            "Paid part.",
            [("charge_payment", {"reservation_id": "PLACEHOLDER", "amount": 640})],
            "Paid the rest.",
        ]
    ) as h:
        reservation_id = await book(h)
        for step in (0, 2):
            h.llm._script[step][0][1]["reservation_id"] = reservation_id

        h.session.start_turn("pay 600")
        required = (await h.next_event("stepup_required")).data
        stepup = h.session.stepups.get(required["stepupId"])
        assert stepup is not None
        h.session.stepups.approve(stepup, fakes.token_set("openid payment:highvalue", client_id=STEPUP_CLIENT))
        await h.drain_turn()

        second = await h.turn("pay the rest")
        assert not of(second, "stepup_required")
        assert of(second, "tool_call_finished")[0]["ok"]
        assert of(second, "token_issued")[0]["strategy"] == "cached"


async def test_idp_refusal_is_reported_without_asking_the_guest(env: Env) -> None:
    async with env.harness(
        [[("create_reservation", {"room_id": "lighthouse-deluxe", **DATES})], "Could not book."]
    ) as h:
        h.thunder.refuse.add("token_exchange")
        events = await h.turn("book")

        assert of(events, "turn_failed") or not of(events, "tool_call_finished")[0]["ok"]
        assert not of(events, "stepup_required")


async def test_expired_login_grant_is_refreshed_before_exchanging(env: Env) -> None:
    async with env.harness([[("create_reservation", {"room_id": "lighthouse-deluxe", **DATES})], "Booked."]) as h:
        old = h.session.broker.grant.tokens
        h.session.broker.grant.tokens = replace(old, expires_at=0)
        events = await h.turn("book")

        assert of(events, "tool_call_finished")[0]["ok"]
        assert ("refresh", frozenset()) in h.thunder.calls
        assert h.session.broker.grant.tokens.refresh_token == "refresh-1"
