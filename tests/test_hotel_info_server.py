from servers import _common
from servers.demo_data import ROOM_TYPES
from servers.hotel_info_server import create_server


async def test_server_exposes_two_read_only_tools() -> None:
    tools = await create_server().list_tools()
    assert sorted(tool.name for tool in tools) == ["get_hotel_info", "list_room_types"]


async def test_get_hotel_info_flags_demo_data() -> None:
    _, info = await create_server().call_tool("get_hotel_info", {})
    assert info["name"] == "Hard Rock Hotel Miami"
    assert (info["check_in_time"], info["check_out_time"]) == ("16:00", "11:00")
    assert "Demo data" in info["notice"]


async def test_list_room_types_hides_inventory() -> None:
    _, result = await create_server().call_tool("list_room_types", {})
    rooms = result["room_types"]
    assert [room["code"] for room in rooms] == [room.code for room in ROOM_TYPES]
    assert all("inventory" not in room for room in rooms)


def test_transport_security_allows_extra_host_with_and_without_port() -> None:
    settings = _common.transport_security(8101, ["192.168.5.2"])
    assert settings is not None
    assert {"192.168.5.2", "192.168.5.2:8101"} <= set(settings.allowed_hosts)
    assert _common.transport_security(8101, []) is None
