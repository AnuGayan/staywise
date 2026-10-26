import json
from pathlib import Path

import pytest

from staywise.config import ConfigError, ServerConfig, load_config


def write_config(tmp_path: Path, servers: object) -> Path:
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def test_loads_http_and_stdio_servers_with_env_expansion(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {
            "toolbox": {
                "url": "https://${GW_HOST}/toolbox/mcp",
                "headers": {"Authorization": "Bearer ${GW_TOKEN}"},
            },
            "notes": {"command": "python", "args": ["servers/notes_server.py", "--transport", "stdio"]},
        },
    )
    configs = load_config(path, environ={"GW_HOST": "gw.local:8243", "GW_TOKEN": "abc"})
    assert configs == [
        ServerConfig(
            name="toolbox",
            url="https://gw.local:8243/toolbox/mcp",
            headers={"Authorization": "Bearer abc"},
        ),
        ServerConfig(
            name="notes",
            command="python",
            args=("servers/notes_server.py", "--transport", "stdio"),
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


CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SERVERS = ["catalog", "reservations", "payments", "concierge"]


def test_shipped_config_points_at_the_four_local_servers() -> None:
    configs = load_config(CONFIG_DIR / "mcp_servers.json", environ={})
    assert [(c.name, c.url) for c in configs] == [
        (name, f"http://127.0.0.1:{port}/mcp") for name, port in zip(SERVERS, range(8101, 8105), strict=True)
    ]


def test_shipped_wso2_config_routes_every_server_through_the_gateway() -> None:
    gateway = "http://default-default.gateway.localhost:19080"
    for environ in ({}, {"WSO2_GATEWAY_URL": gateway}):
        configs = load_config(CONFIG_DIR / "mcp_servers.wso2.json", environ=environ)
        assert [(c.name, c.url) for c in configs] == [(name, f"{gateway}/staywise-{name}/mcp") for name in SERVERS]
    custom = load_config(CONFIG_DIR / "mcp_servers.wso2.json", environ={"WSO2_GATEWAY_URL": "http://gw.test"})
    assert custom[0].url == "http://gw.test/staywise-catalog/mcp"


@pytest.mark.parametrize(
    ("environ", "expected"), [({}, "http://d/mcp"), ({"H": ""}, "http://d/mcp"), ({"H": "x"}, "x/mcp")]
)
def test_default_applies_when_var_is_unset_or_empty(tmp_path: Path, environ: dict[str, str], expected: str) -> None:
    path = write_config(tmp_path, {"t": {"url": "${H:-http://d}/mcp"}})
    assert load_config(path, environ=environ)[0].url == expected
