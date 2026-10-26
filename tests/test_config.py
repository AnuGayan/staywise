import json
from pathlib import Path

import pytest

from concierge.config import DEFAULT_ENV_PREFIXES, ConfigError, ServerConfig, load_config, load_env_servers


def write_config(tmp_path: Path, servers: object) -> Path:
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def test_loads_http_and_stdio_servers_with_env_expansion(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {
            "reservations": {
                "url": "https://${GW_HOST}/reservations/mcp",
                "headers": {"Authorization": "Bearer ${GW_TOKEN}"},
            },
            "unity-loyalty": {"command": "python", "args": ["servers/unity_server.py", "--transport", "stdio"]},
        },
    )
    configs = load_config(path, environ={"GW_HOST": "gw.local:8243", "GW_TOKEN": "abc"})
    assert configs == [
        ServerConfig(
            name="reservations",
            url="https://gw.local:8243/reservations/mcp",
            headers={"Authorization": "Bearer abc"},
        ),
        ServerConfig(
            name="unity-loyalty",
            command="python",
            args=("servers/unity_server.py", "--transport", "stdio"),
            cwd=tmp_path,
        ),
    ]


def test_missing_env_var_is_reported_by_name(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"t": {"url": "http://x/mcp", "headers": {"Authorization": "Bearer ${NOPE}"}}})
    with pytest.raises(ConfigError, match=r"\$\{NOPE\}"):
        load_config(path, environ={})


@pytest.mark.parametrize(
    "servers",
    [
        {},
        {"t": {}},
        {"t": {"url": "http://x/mcp", "command": "python"}},
        {"t": "http://x/mcp"},
    ],
)
def test_invalid_entries_are_rejected(tmp_path: Path, servers: object) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, servers), environ={})


def test_unreadable_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(bad)


def shipped(name: str, environ: dict[str, str]) -> list[tuple[str, str | None]]:
    configs = load_config(Path(__file__).resolve().parent.parent / name, environ=environ)
    return [(c.name, c.url) for c in configs]


def test_shipped_config_points_at_the_three_local_servers() -> None:
    assert shipped("mcp_servers.json", {}) == [
        ("hotel-info", "http://127.0.0.1:8101/mcp"),
        ("reservations", "http://127.0.0.1:8102/mcp"),
        ("unity-loyalty", "http://127.0.0.1:8103/mcp"),
    ]


def test_shipped_remote_config_uses_mcp_host() -> None:
    assert shipped("mcp_servers.remote.json", {"MCP_HOST": "192.168.5.2"}) == [
        ("hotel-info", "http://192.168.5.2:8101/mcp"),
        ("reservations", "http://192.168.5.2:8102/mcp"),
        ("unity-loyalty", "http://192.168.5.2:8103/mcp"),
    ]


def test_shipped_gateway_config_routes_every_server_through_the_gateway() -> None:
    gateway = "http://default-default.gateway.localhost:19080"
    assert shipped("mcp_servers.gateway.json", {"WSO2_GATEWAY_URL": gateway}) == [
        ("hotel-info", f"{gateway}/hotel-info/mcp"),
        ("reservations", f"{gateway}/reservations/mcp"),
        ("unity-loyalty", f"{gateway}/unity/mcp"),
    ]


def test_env_servers_follow_agent_manager_variables() -> None:
    env = {
        "HOTEL_INFO_URL": " https://gw/hotel-info/mcp ",
        "HOTEL_INFO_API_KEY": "k1",
        "UNITY_LOYALTY_URL": "https://gw/unity/mcp",
        "UNITY_LOYALTY_API_KEY": " k3 ",
    }
    configs = load_env_servers(DEFAULT_ENV_PREFIXES, env)
    assert configs == [
        ServerConfig(
            name="hotel-info", url="https://gw/hotel-info/mcp", headers={"API-Key": "k1", "Authorization": ""}
        ),
        ServerConfig(name="unity-loyalty", url="https://gw/unity/mcp", headers={"API-Key": "k3", "Authorization": ""}),
    ]


def test_env_servers_split_comma_separated_urls() -> None:
    env = {"RESERVATIONS_URL": "https://a/mcp, ,https://b/mcp", "RESERVATIONS_API_KEY": "k"}
    assert [(c.name, c.url) for c in load_env_servers(["RESERVATIONS"], env)] == [
        ("reservations-1", "https://a/mcp"),
        ("reservations-2", "https://b/mcp"),
    ]


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"RESERVATIONS_URL": "https://a/mcp"}, "RESERVATIONS_API_KEY is not set, but RESERVATIONS_URL is"),
        ({"RESERVATIONS_API_KEY": "k"}, "RESERVATIONS_URL is not set, but RESERVATIONS_API_KEY is"),
        ({}, "no MCP servers configured in the environment; expected some of: RESERVATIONS_URL"),
    ],
)
def test_env_servers_report_incomplete_configuration(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_env_servers(["RESERVATIONS"], env)


def test_env_servers_reject_bad_prefixes() -> None:
    with pytest.raises(ConfigError, match="upper-case"):
        load_env_servers(["unity-loyalty"], {})
