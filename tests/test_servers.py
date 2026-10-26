"""End-to-end through the guard and FastMCP, over HTTP in memory: auth challenges and every tool."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from servers import catalog_server, concierge_server, payments_server, reservations_server
from servers.runner import GuardSettings, guarded_app
from servers.store import HotelStore
from tests.conftest import AGENT_ID, STEPUP_CLIENT, StaticKeyVerifier, TokenFactory

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class McpCaller:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._next_id = 0

    async def call(
        self, tool: str, arguments: dict[str, Any], token: str | None, header: str = "Authorization"
    ) -> httpx.Response:
        self._next_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        headers = dict(HEADERS)
        if token:
            headers[header] = f"Bearer {token}"
        return await self._client.post("/mcp", json=body, headers=headers)

    async def result(self, tool: str, arguments: dict[str, Any], token: str) -> dict[str, Any]:
        response = await self.call(tool, arguments, token)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert not result.get("isError"), result
        return result["structuredContent"]


Module = Any


@asynccontextmanager
async def serve(module: Module, store: HotelStore, settings: GuardSettings) -> AsyncIterator[McpCaller]:
    server = module.create_server(store)
    app = guarded_app(server, module.create_rules(store, settings), settings, verifier=StaticKeyVerifier())
    async with server.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8100") as client:
            yield McpCaller(client)


def challenge(response: httpx.Response) -> str:
    return response.headers.get("www-authenticate", "")


Serve = Callable[[Module], Any]


@pytest.fixture
def serve_module(store: HotelStore, guard_settings: GuardSettings) -> Serve:
    return lambda module: serve(module, store, guard_settings)


async def test_missing_token_gets_401_with_metadata_pointer(serve_module: Serve) -> None:
    async with serve_module(catalog_server) as mcp:
        response = await mcp.call("search_hotels", {}, token=None)
    assert response.status_code == 401
    assert 'error="invalid_token"' in challenge(response)
    assert 'resource_metadata="http://127.0.0.1:8100/.well-known/oauth-protected-resource"' in challenge(response)


async def test_token_for_another_audience_is_rejected(serve_module: Serve, token: TokenFactory) -> None:
    async with serve_module(catalog_server) as mcp:
        response = await mcp.call("search_hotels", {}, token("hotel:read", aud="urn:other"))
    assert response.status_code == 401


async def test_token_forwarded_by_the_wso2_gateway_is_verified(serve_module: Serve, token: TokenFactory) -> None:
    # mcp-auth with forwardToken: true sends the token upstream as X-Forwarded-Authorization.
    async with serve_module(catalog_server) as mcp:
        accepted = await mcp.call("search_hotels", {}, token("hotel:read"), header="X-Forwarded-Authorization")
        foreign = await mcp.call(
            "search_hotels", {}, token("hotel:read", aud="urn:other"), header="X-Forwarded-Authorization"
        )
        unscoped = await mcp.call("search_hotels", {}, token("booking:read"), header="X-Forwarded-Authorization")
    assert accepted.status_code == 200 and not accepted.json()["result"].get("isError")
    assert foreign.status_code == 401
    assert unscoped.status_code == 403


async def test_authorization_header_wins_over_forwarded_one(serve_module: Serve, token: TokenFactory) -> None:
    async with serve_module(catalog_server) as mcp:
        response = await mcp._client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "search_hotels", "arguments": {}},
            },
            headers={
                **HEADERS,
                "Authorization": f"Bearer {token('booking:read')}",
                "X-Forwarded-Authorization": f"Bearer {token('hotel:read')}",
            },
        )
    assert response.status_code == 403


async def test_protected_resource_metadata(serve_module: Serve) -> None:
    async with serve_module(catalog_server) as mcp:
        response = await mcp._client.get("/.well-known/oauth-protected-resource")
    assert response.json()["authorization_servers"] == ["http://idp.test"]
    assert response.json()["resource"] == "http://127.0.0.1:8100/mcp"


async def test_catalog_search_filters_and_availability(serve_module: Serve, token: TokenFactory) -> None:
    async with serve_module(catalog_server) as mcp:
        found = await mcp.result(
            "search_hotels",
            {
                "city": "Galle",
                "max_nightly_rate": 200,
                "sea_view": True,
                "check_in": "2026-11-14",
                "check_out": "2026-11-16",
            },
            token("hotel:read"),
        )
        quote = await mcp.result(
            "check_availability",
            {"room_id": "lighthouse-deluxe", "check_in": "2026-11-14", "check_out": "2026-11-16"},
            token("hotel:read"),
        )
    assert [room["id"] for room in found["rooms"]] == ["rampart-double", "lighthouse-deluxe"]
    assert quote["total"] == 360 and quote["due_at_booking"] == 90


async def test_wrong_scope_gets_insufficient_scope_challenge(serve_module: Serve, token: TokenFactory) -> None:
    async with serve_module(reservations_server) as mcp:
        response = await mcp.call(
            "create_reservation",
            {"room_id": "lighthouse-deluxe", "check_in": "2026-11-14", "check_out": "2026-11-16"},
            token("booking:read"),
        )
    assert response.status_code == 403
    assert 'error="insufficient_scope", scope="booking:write"' in challenge(response)


async def test_unknown_tool_is_denied(serve_module: Serve, token: TokenFactory) -> None:
    async with serve_module(catalog_server) as mcp:
        response = await mcp.call("drop_tables", {}, token("hotel:read booking:write"))
    assert response.status_code == 403


async def booking(mcp: McpCaller, token: TokenFactory, room: str = "lighthouse-deluxe") -> dict[str, Any]:
    return await mcp.result(
        "create_reservation",
        {"room_id": room, "check_in": "2026-11-14", "check_out": "2026-11-16"},
        token("booking:write", act={"sub": AGENT_ID}),
    )


async def test_small_charge_is_delegated_large_charge_needs_mfa_token(
    serve_module: Serve, token: TokenFactory, store: HotelStore
) -> None:
    async with serve_module(reservations_server) as reservations:
        reservation = await booking(reservations, token)
        await reservations.result(
            "change_room", {"reservation_id": reservation["id"], "new_room_id": "ocean-suite"}, token("booking:write")
        )
    assert reservation["deposit_due"] == 90

    async with serve_module(payments_server) as payments:
        delegated = token("payment:charge", act={"sub": AGENT_ID})
        small = await payments.result("charge_payment", {"reservation_id": reservation["id"], "amount": 90}, delegated)
        refused = await payments.call(
            "charge_payment", {"reservation_id": reservation["id"], "amount": 1150}, delegated
        )
        wrong_app = await payments.call(
            "charge_payment", {"reservation_id": reservation["id"], "amount": 1150}, token("payment:highvalue")
        )
        mfa = token("payment:highvalue", client_id=STEPUP_CLIENT)
        large = await payments.result(
            "charge_payment", {"reservation_id": reservation["id"], "amount": 1150, "purpose": "prepayment"}, mfa
        )

    assert small["payment"]["acting_agent"] == AGENT_ID
    assert refused.status_code == 403
    assert 'scope="payment:highvalue"' in challenge(refused)
    assert "over $500" in challenge(refused)
    assert wrong_app.status_code == 403
    assert large["payment"]["authorized_client"] == STEPUP_CLIENT
    assert large["payment"]["acting_agent"] is None
    assert large["reservation"]["balance_due"] == 0


async def test_cancelling_prepaid_nonrefundable_needs_mfa(serve_module: Serve, token: TokenFactory) -> None:
    async with serve_module(reservations_server) as reservations:
        reservation = await booking(reservations, token, room="ocean-suite")
    async with serve_module(payments_server) as payments:
        await payments.result(
            "charge_payment",
            {"reservation_id": reservation["id"], "amount": 1240},
            token("payment:highvalue", client_id=STEPUP_CLIENT),
        )
    async with serve_module(reservations_server) as reservations:
        refused = await reservations.call(
            "cancel_reservation", {"reservation_id": reservation["id"]}, token("booking:write")
        )
        cancelled = await reservations.result(
            "cancel_reservation",
            {"reservation_id": reservation["id"]},
            token("payment:highvalue", client_id=STEPUP_CLIENT),
        )
    assert refused.status_code == 403 and 'scope="payment:highvalue"' in challenge(refused)
    assert cancelled["status"] == "cancelled"


async def test_guest_cannot_see_another_guests_reservation(serve_module: Serve, token: TokenFactory) -> None:
    async with serve_module(reservations_server) as reservations:
        reservation = await booking(reservations, token)
        mine = await reservations.result("list_my_reservations", {}, token("booking:read"))
        theirs = await reservations.result("list_my_reservations", {}, token("booking:read", sub="someone-else"))
        response = await reservations.call(
            "cancel_reservation", {"reservation_id": reservation["id"]}, token("booking:write", sub="someone-else")
        )
    assert [r["id"] for r in mine["reservations"]] == [reservation["id"]]
    assert theirs["reservations"] == []
    assert response.json()["result"]["isError"] is True


async def test_loyalty_and_concierge(serve_module: Serve, token: TokenFactory) -> None:
    async with serve_module(payments_server) as payments:
        balance = await payments.result("get_loyalty_balance", {}, token("booking:read"))
        redeemed = await payments.result("redeem_points", {"points": 500}, token("payment:charge"))
    async with serve_module(concierge_server) as concierge:
        user_try = await concierge.call("notify_front_desk", {"message": "late"}, token("booking:write"))
        note = await concierge.result(
            "notify_front_desk", {"message": "Arriving 11pm"}, token("ops:notify", sub=AGENT_ID, client_id=AGENT_ID)
        )
    assert balance["points"] == 12_500 and redeemed["remaining_points"] == 12_000
    assert user_try.status_code == 403 and 'scope="ops:notify"' in challenge(user_try)
    assert note["requested_by"] == AGENT_ID and json.dumps(note)
