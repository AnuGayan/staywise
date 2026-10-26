"""The web backend over HTTP (in memory): sign-in, step-up popup round trip, chat, mock phone."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.requests import Request

from servers.runner import GuardSettings
from servers.store import HotelStore
from staywise.http_api import SESSION_COOKIE, Backend, create_app
from staywise.session import ChatSession, SessionDeps
from tests import fakes
from tests.conftest import STEPUP_CLIENT, WEB_CLIENT

BASE = "http://localhost:9100"


@dataclass
class App:
    client: httpx.AsyncClient
    backend: Backend
    thunder: fakes.FakeThunder

    @property
    def session(self) -> ChatSession:
        return next(iter(self.backend.sessions.values()))

    async def sign_in(self) -> None:
        login = await self.client.get("/auth/login")
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        callback = await self.client.get("/auth/callback", params={"code": "good", "state": state})
        assert callback.status_code == 302, callback.text


@asynccontextmanager
async def running(
    tmp_path: Path, store: HotelStore, guard: GuardSettings, script: list[Any], public_url: str = BASE
) -> AsyncIterator[App]:
    async with fakes.mcp_servers(store, guard) as router:
        thunder = fakes.FakeThunder()
        deps = SessionDeps(
            settings=fakes.settings(tmp_path, PUBLIC_URL=public_url),
            thunder=thunder,
            policy=fakes.policy(),
            llm=fakes.ScriptedLLM(script),
            model="test",
            mcp_configs=fakes.server_configs(),
            inner_transport=router,
        )
        backend = Backend(deps)
        transport = httpx.ASGITransport(app=create_app(backend))
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
            try:
                yield App(client, backend, thunder)
            finally:
                await backend.close()


@pytest.fixture
def app_factory(tmp_path: Path, store: HotelStore, guard_settings: GuardSettings) -> Any:
    return lambda script=(), public_url=BASE: running(tmp_path, store, guard_settings, list(script), public_url)


async def test_login_redirects_to_thunder_with_pkce_and_resource(app_factory: Any) -> None:
    async with app_factory() as app:
        response = await app.client.get("/auth/login")
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert response.status_code == 302
    assert response.headers["location"].startswith("http://idp.test/oauth2/authorize?")
    assert query["client_id"] == [WEB_CLIENT]
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == ["urn:staywise:hotel-api"]
    assert "payment:highvalue" not in query["scope"][0]
    assert query["redirect_uri"] == [f"{BASE}/auth/callback"]


async def test_callback_with_unknown_state_is_rejected(app_factory: Any) -> None:
    async with app_factory() as app:
        response = await app.client.get("/auth/callback", params={"code": "x", "state": "forged"})
    assert response.status_code == 400
    assert not app.backend.sessions


async def test_sign_in_creates_session_and_me_shows_consented_scopes(app_factory: Any) -> None:
    async with app_factory() as app:
        assert (await app.client.get("/api/me")).status_code == 401
        await app.sign_in()
        me = (await app.client.get("/api/me")).json()
        cookie = app.client.cookies.get(SESSION_COOKIE)
    assert me["user"] == {"sub": "user-guest", "name": "Demo Guest"}
    assert "booking:write" in me["grantedScopes"] and "payment:highvalue" not in me["grantedScopes"]
    assert cookie


async def test_chat_runs_a_turn_and_rejects_overlap(app_factory: Any) -> None:
    async with app_factory(["Hello! How can I help?"]) as app:
        await app.sign_in()
        queue = app.session.bus.subscribe()
        accepted = await app.client.post("/api/chat", json={"message": "hi"})
        overlap = await app.client.post("/api/chat", json={"message": "again"})
        types = []
        while "turn_finished" not in types:
            types.append((await asyncio.wait_for(queue.get(), 5)).type)
        empty = await app.client.post("/api/chat", json={"message": "  "})
    assert accepted.status_code == 202 and accepted.json()["turnId"]
    assert overlap.status_code == 409
    assert types == ["turn_started", "assistant_message", "turn_finished"]
    assert empty.status_code == 400


async def start_stepup(app: App) -> tuple[str, asyncio.Task[Any]]:
    """Pause a fake high-value request on the session's coordinator, as the transport would."""
    queue = app.session.bus.subscribe()
    waiter = asyncio.create_task(app.session.stepups.request(frozenset({"payment:highvalue"}), "big charge", None))
    event = await asyncio.wait_for(queue.get(), 5)
    assert event.type == "stepup_required"
    return event.data["stepupId"], waiter


async def popup(app: App, stepup_id: str) -> str:
    start = await app.client.get(f"/auth/stepup/start/{stepup_id}")
    assert start.status_code == 302
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["client_id"] == [STEPUP_CLIENT]
    assert query["acr_values"] == ["mfa"] and query["prompt"] == ["login"]
    assert query["scope"] == ["openid payment:highvalue"]
    return query["state"][0]


async def test_stepup_popup_round_trip_approves(app_factory: Any) -> None:
    async with app_factory() as app:
        await app.sign_in()
        stepup_id, waiter = await start_stepup(app)
        state = await popup(app, stepup_id)
        page = await app.client.get("/auth/stepup/callback", params={"code": "good", "state": state})
        tokens = await asyncio.wait_for(waiter, 5)
    assert "Verified" in page.text and "window.close" in page.text
    assert tokens.claims["client_id"] == STEPUP_CLIENT


async def test_stepup_by_a_different_account_is_rejected(app_factory: Any) -> None:
    async with app_factory() as app:
        await app.sign_in()
        stepup_id, waiter = await start_stepup(app)
        state = await popup(app, stepup_id)
        page = await app.client.get("/auth/stepup/callback", params={"code": "other-user", "state": state})
        with pytest.raises(Exception, match="different account"):
            await asyncio.wait_for(waiter, 5)
    assert "Verification failed" in page.text


async def test_stepup_cancel_and_idp_error_decline(app_factory: Any) -> None:
    async with app_factory() as app:
        await app.sign_in()
        first, waiter_one = await start_stepup(app)
        cancelled = await app.client.post(f"/api/stepup/{first}/cancel")
        second, waiter_two = await start_stepup(app)
        state = await popup(app, second)
        denied = await app.client.get("/auth/stepup/callback", params={"error": "access_denied", "state": state})
        results = await asyncio.gather(waiter_one, waiter_two, return_exceptions=True)
    assert cancelled.status_code == 204
    assert "cancelled" in denied.text
    assert all("declined" in str(result) for result in results)


async def test_stepup_endpoints_need_the_owning_session(app_factory: Any) -> None:
    async with app_factory() as app:
        await app.sign_in()
        stepup_id, waiter = await start_stepup(app)
        app.client.cookies.clear()
        start = await app.client.get(f"/auth/stepup/start/{stepup_id}")
        cancel = await app.client.post(f"/api/stepup/{stepup_id}/cancel")
        waiter.cancel()
    assert start.status_code == 404 and cancel.status_code == 401


async def test_mock_phone_requires_key_and_broadcasts_otp(app_factory: Any) -> None:
    async with app_factory() as app:
        await app.sign_in()
        queue = app.session.bus.subscribe()
        rejected = await app.client.post("/mock-phone/sms", json={"message": "123456"})
        accepted = await app.client.post(
            "/mock-phone/sms",
            json={"to": "+94770000000", "body": "Your StayWise verification code is 482913"},
            headers={"X-Mock-Phone-Key": "phone-key"},
        )
        event = await asyncio.wait_for(queue.get(), 5)
        inbox = (await app.client.get("/api/mock-phone/messages")).json()
    assert rejected.status_code == 401 and accepted.status_code == 200
    assert event.type == "phone_message" and event.data["otp"] == "482913"
    assert inbox[0]["text"] == "Your StayWise verification code is 482913"


async def test_logout_ends_the_session(app_factory: Any) -> None:
    async with app_factory() as app:
        await app.sign_in()
        assert (await app.client.post("/auth/logout")).status_code == 204
        assert not app.backend.sessions
        assert (await app.client.get("/api/me")).status_code == 401


async def test_urls_carry_the_path_prefix_when_served_under_one(app_factory: Any) -> None:
    # Agent Manager serves the agent at /<routePath> and strips that prefix before the app sees the request.
    async with app_factory(public_url=f"{BASE}/staywise") as app:
        anonymous = await app.client.get("/api/me")
        login = await app.client.get("/auth/login")
        query = parse_qs(urlparse(login.headers["location"]).query)
        callback = await app.client.get("/auth/callback", params={"code": "good", "state": query["state"][0]})
        stepup_id, waiter = await start_stepup(app)
        replay = app.session.bus.subscribe(after=0)
        events = [replay.get_nowait() for _ in range(replay.qsize())]
        start_url = next(e for e in events if e.type == "stepup_required").data["startUrl"]
        app.session.stepups.decline(app.session.stepups.get(stepup_id))
        await asyncio.gather(waiter, return_exceptions=True)
    assert anonymous.json()["loginUrl"] == "/staywise/auth/login"
    assert query["redirect_uri"] == [f"{BASE}/staywise/auth/callback"]
    assert callback.headers["location"] == f"{BASE}/staywise/"
    assert "Path=/staywise" in callback.headers["set-cookie"]
    assert start_url == f"/staywise/auth/stepup/start/{stepup_id}"


async def test_events_reconnect_replays_what_was_missed(app_factory: Any) -> None:
    async with app_factory() as app:
        await app.sign_in()
        bus = app.session.bus
        for n in range(3):
            bus.publish("turn_started", {"turnId": f"t{n}", "message": "m"})
        cookie = f"{SESSION_COOKIE}={app.client.cookies.get(SESSION_COOKIE)}"
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/events",
            "headers": [(b"cookie", cookie.encode()), (b"last-event-id", b"1")],
        }
        response = await app.backend.events(Request(scope))
        stream = response.body_iterator
        first = [await anext(stream) for _ in range(3)]
        await stream.aclose()
    assert first[0].retry == 1000
    assert [(e.id, e.event) for e in first[1:]] == [("2", "turn_started"), ("3", "turn_started")]
