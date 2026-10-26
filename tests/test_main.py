from pathlib import Path

import pytest
from ollama import AsyncClient, ResponseError

from concierge.__main__ import DEFAULT_MODEL, DEFAULT_PORT, answer, open_llm, parse_args, print_tool_call, run
from concierge.agent import AgentStepLimitError
from concierge.openai_compat import OpenAIChatClient


class FailingAgent:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def ask(self, prompt: str) -> str:
        raise self._error


def test_parse_args_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("CONCIERGE_MEMBER_ID", raising=False)
    args = parse_args([])
    assert args.model == DEFAULT_MODEL
    assert args.config.name == "mcp_servers.json"
    assert args.prompt is None
    assert (args.serve, args.host, args.port) == (False, "127.0.0.1", DEFAULT_PORT)
    assert args.member_id == "HR-100245"


def test_parse_args_reads_member_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONCIERGE_MEMBER_ID", "HR-100512")
    assert parse_args([]).member_id == "HR-100512"
    assert parse_args(["--member-id", "HR-100377"]).member_id == "HR-100377"


def test_parse_args_reads_model_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1:8b")
    assert parse_args([]).model == "llama3.1:8b"
    assert parse_args(["-m", "qwen3:4b"]).model == "qwen3:4b"


def test_print_tool_call_truncates_long_results(capsys: pytest.CaptureFixture[str]) -> None:
    print_tool_call("list_bookings", {"query": "x"}, "y" * 500)
    line = capsys.readouterr().out.strip()
    assert line.startswith('↳ list_bookings({"query": "x"}) → yyy')
    assert line.endswith("...")
    assert len(line) < 220


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ResponseError("model 'nope' not found", 404), "ollama pull nope"),
        (ConnectionError("refused"), "is Ollama / the gateway running"),
        (AgentStepLimitError("no final answer"), "no final answer"),
    ],
)
async def test_answer_reports_failures(error: Exception, message: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert await answer(FailingAgent(error), "hi", "nope") is False  # type: ignore[arg-type]
    assert message in capsys.readouterr().err


async def test_run_exits_2_on_bad_config(tmp_path: Path) -> None:
    assert await run(parse_args(["-c", str(tmp_path / "missing.json"), "-p", "hi"])) == 2


async def test_run_exits_1_when_servers_are_down(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "servers.json"
    config.write_text('{"mcpServers": {"reservations": {"url": "http://127.0.0.1:9/mcp"}}}', encoding="utf-8")
    assert await run(parse_args(["-c", str(config), "-p", "hi"])) == 1
    assert "cannot connect to MCP server 'reservations'" in capsys.readouterr().err


async def test_open_llm_picks_client_from_flags() -> None:
    async with open_llm(parse_args([])) as llm:
        assert isinstance(llm, AsyncClient)
    async with open_llm(parse_args(["--llm-base-url", "http://gw/ollama"])) as llm:
        assert isinstance(llm, OpenAIChatClient)


async def test_serve_exits_2_without_sign_in_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CONCIERGE_CLIENT_SECRET", raising=False)
    assert await run(parse_args(["--serve"])) == 2
    assert "CONCIERGE_CLIENT_SECRET is not set" in capsys.readouterr().err


async def test_serve_exits_2_on_unreadable_member_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CONCIERGE_CLIENT_SECRET", "x")
    assert await run(parse_args(["--serve", "--members", str(tmp_path / "none.json")])) == 2
    assert "cannot read member map" in capsys.readouterr().err


def test_mcp_from_env_flag_and_prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_FROM_ENV", raising=False)
    monkeypatch.delenv("MCP_ENV_PREFIXES", raising=False)
    args = parse_args([])
    assert (args.mcp_from_env, args.mcp_env_prefixes) == (False, "HOTEL_INFO,RESERVATIONS,UNITY_LOYALTY")
    monkeypatch.setenv("MCP_FROM_ENV", "true")
    monkeypatch.setenv("MCP_ENV_PREFIXES", "UNITY_LOYALTY")
    args = parse_args([])
    assert (args.mcp_from_env, args.mcp_env_prefixes) == (True, "UNITY_LOYALTY")


async def test_run_exits_2_when_env_has_no_mcp_servers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for prefix in ("HOTEL_INFO", "RESERVATIONS", "UNITY_LOYALTY"):
        monkeypatch.delenv(f"{prefix}_URL", raising=False)
        monkeypatch.delenv(f"{prefix}_API_KEY", raising=False)
    assert await run(parse_args(["--mcp-from-env", "-p", "hi"])) == 2
    assert "no MCP servers configured in the environment" in capsys.readouterr().err
