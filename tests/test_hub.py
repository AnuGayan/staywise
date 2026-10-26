from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, types
from mcp.shared.memory import create_connected_server_and_client_session

from concierge.config import ServerConfig
from concierge.hub import BearerAuth, McpConnectionError, McpHub, _hint, connect, format_tool_result
from servers import hotel_info_server, reservations_server, unity_server

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def all_servers(tmp_path: Path) -> AsyncIterator[McpHub]:
    """A hub over in-memory sessions to all three servers (a context manager, not a fixture,
    because anyio cancel scopes must be exited by the task that entered them)."""
    hotel = hotel_info_server.create_server()
    bookings = reservations_server.create_server(
        reservations_server.BookingStore(tmp_path / "r.db", today=lambda: date(2026, 10, 26))
    )
    unity = unity_server.create_server(unity_server.LoyaltyStore(tmp_path / "u.db"))
    async with (
        create_connected_server_and_client_session(hotel._mcp_server) as hotel_session,
        create_connected_server_and_client_session(bookings._mcp_server) as bookings_session,
        create_connected_server_and_client_session(unity._mcp_server) as unity_session,
    ):
        hub = McpHub()
        await hub.add_server("hotel-info", hotel_session)
        await hub.add_server("reservations", bookings_session)
        await hub.add_server("unity-loyalty", unity_session)
        yield hub


async def test_hub_merges_all_nine_tools(tmp_path: Path) -> None:
    async with all_servers(tmp_path) as hub:
        assert hub.tool_names == [
            "get_hotel_info",
            "list_room_types",
            "check_availability",
            "list_bookings",
            "get_booking",
            "create_booking",
            "cancel_booking",
            "get_unity_balance",
            "get_points_activity",
        ]


async def test_ollama_tools_use_function_schema(tmp_path: Path) -> None:
    async with all_servers(tmp_path) as hub:
        create = next(t for t in hub.ollama_tools() if t["function"]["name"] == "create_booking")
        assert create["type"] == "function"
        assert "Book one room" in create["function"]["description"]
        assert create["function"]["parameters"]["required"] == [
            "member_id",
            "room_code",
            "check_in",
            "check_out",
            "guests",
        ]


async def test_call_tool_routes_to_owning_server(tmp_path: Path) -> None:
    async with all_servers(tmp_path) as hub:
        assert '"name": "Hard Rock Hotel Miami"' in await hub.call_tool("get_hotel_info", {})
        assert '"points_balance": 18450' in await hub.call_tool("get_unity_balance", {"member_id": "HR-100245"})
        booked = await hub.call_tool(
            "create_booking",
            {
                "member_id": "HR-100245",
                "room_code": "DLXK",
                "check_in": "2026-12-12",
                "check_out": "2026-12-14",
                "guests": 2,
            },
        )
        assert '"status": "upcoming"' in booked


async def test_call_tool_reports_tool_errors_as_text(tmp_path: Path) -> None:
    async with all_servers(tmp_path) as hub:
        result = await hub.call_tool("get_unity_balance", {"member_id": "HR-999"})
        assert (
            result
            == "Error executing tool get_unity_balance: unknown Unity member id 'HR-999'; ids look like HR-100245"
        )


async def test_call_tool_reports_unknown_tool(tmp_path: Path) -> None:
    async with all_servers(tmp_path) as hub:
        result = await hub.call_tool("launch_rocket", {})
        assert result.startswith("Error: unknown tool 'launch_rocket'")
        assert "create_booking" in result


async def test_duplicate_tool_names_are_rejected() -> None:
    async with (
        create_connected_server_and_client_session(hotel_info_server.create_server()._mcp_server) as first,
        create_connected_server_and_client_session(hotel_info_server.create_server()._mcp_server) as second,
    ):
        hub = McpHub()
        await hub.add_server("a", first)
        with pytest.raises(ValueError, match="clashes"):
            await hub.add_server("b", second)


def test_format_tool_result_handles_non_text_blocks() -> None:
    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="chart:"),
            types.ImageContent(type="image", data="", mimeType="image/png"),
        ]
    )
    assert format_tool_result(result) == "chart:\n[image content omitted]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("boom", "Error: boom"), ("Error executing tool x: boom", "Error executing tool x: boom")],
)
def test_format_tool_result_marks_errors_once(text: str, expected: str) -> None:
    result = types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)
    assert format_tool_result(result) == expected


async def test_connect_names_the_unreachable_server() -> None:
    configs = [ServerConfig(name="reservations", url="http://127.0.0.1:9/mcp")]
    with pytest.raises(McpConnectionError, match="'reservations'"):
        async with connect(configs):
            pytest.fail("should not connect")


@pytest.mark.parametrize(
    ("status", "hint"),
    [(401, "rejected the credentials"), (403, "rejected the credentials"), (404, "check the path"), (500, "running")],
)
def test_hint_matches_the_failure(status: int, hint: str) -> None:
    request = httpx.Request("POST", "http://gw/mcp")
    error = httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status, request=request))
    assert hint in _hint(error)
    assert "running" in _hint(httpx.ConnectError("refused"))


async def test_connect_over_stdio_negotiates_2025_06_18() -> None:
    configs = [
        ServerConfig(
            name="hotel-info",
            command="python",
            args=("-m", "servers.hotel_info_server", "--transport", "stdio"),
            cwd=PROJECT_ROOT,
        )
    ]
    async with connect(configs) as hub:
        assert hub.protocol_versions == {"hotel-info": "2025-06-18"}
        assert '"code": "DLXK"' in await hub.call_tool("list_room_types", {})


def test_bearer_auth_sets_the_header_only_while_a_token_is_set() -> None:
    auth = BearerAuth()
    request = httpx.Request("POST", "http://gw/mcp", headers={"Authorization": ""})
    assert next(auth.auth_flow(request)).headers["Authorization"] == ""
    auth.token = "t1"
    assert next(auth.auth_flow(request)).headers["Authorization"] == "Bearer t1"


async def test_bearer_is_set_for_the_call_and_cleared_after(tmp_path: Path) -> None:
    seen: list[str | None] = []

    class SpyAuth(BearerAuth):
        pass

    spy = SpyAuth()
    unity = unity_server.create_server(unity_server.LoyaltyStore(tmp_path / "u.db"))
    async with create_connected_server_and_client_session(unity._mcp_server) as session:
        hub = McpHub()
        await hub.add_server("unity-loyalty", session, spy)
        original = session.call_tool

        async def call_and_peek(name: str, arguments: dict[str, object] | None = None) -> types.CallToolResult:
            seen.append(spy.token)
            return await original(name, arguments)

        session.call_tool = call_and_peek  # type: ignore[method-assign]
        result = await hub.call_tool("get_unity_balance", {"member_id": "HR-100245"}, bearer="exchanged")
        assert '"points_balance": 18450' in result
        assert seen == ["exchanged"]
        assert spy.token is None
        await hub.call_tool("get_unity_balance", {"member_id": "HR-100245"})
        assert seen == ["exchanged", None]


async def test_connect_sends_the_startup_token_only_during_the_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    from concierge import hub as hub_module

    hotel = hotel_info_server.create_server()
    seen: list[str | None] = []
    auth = BearerAuth()

    async def fake_open_session(stack: AsyncExitStack, config: ServerConfig) -> tuple[ClientSession, BearerAuth]:
        session = await stack.enter_async_context(create_connected_server_and_client_session(hotel._mcp_server))
        original = session.initialize

        async def initialize() -> types.InitializeResult:
            seen.append(auth.token)
            return await original()

        session.initialize = initialize  # type: ignore[method-assign]
        return session, auth

    monkeypatch.setattr(hub_module, "_open_session", fake_open_session)
    async with connect([ServerConfig(name="hotel-info", url="http://gw/mcp")], startup_token="agent-at") as hub:
        assert seen == ["agent-at"]
        assert auth.token is None
        assert "get_hotel_info" in hub.tool_names
