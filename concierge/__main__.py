"""Run the concierge as a CLI chat (`python -m concierge [-p PROMPT]`) or as the agent HTTP API (`--serve`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from ollama import AsyncClient, ResponseError

from concierge.agent import (
    DEFAULT_MAX_STEPS,
    DEFAULT_MEMBER_ID,
    Agent,
    AgentStepLimitError,
    ChatClient,
    ToolCallObserver,
    build_system_prompt,
)
from concierge.config import DEFAULT_ENV_PREFIXES, ConfigError, ServerConfig, load_config, load_env_servers
from concierge.http_api import Runtime, create_app
from concierge.hub import McpConnectionError, McpHub, connect
from concierge.identity import Guest, MemberMapError, load_members
from concierge.openai_compat import DEFAULT_API_KEY_HEADER, OpenAIChatClient
from concierge.tokens import AgentIdentity, GuestTools, TokenBroker, TokenConfigError, TokenError

DEFAULT_MODEL = "qwen3:1.7b"
DEFAULT_PORT = 8000
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "mcp_servers.json"
DEFAULT_MEMBERS = Path(__file__).resolve().parent.parent / "config" / "members.json"
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line flags; OLLAMA_MODEL, OLLAMA_HOST, LLM_BASE_URL and CONCIERGE_MEMBER_ID provide defaults."""
    parser = argparse.ArgumentParser(
        prog="concierge", description="Hard Rock Hotel Miami concierge agent over MCP tools."
    )
    parser.add_argument("-m", "--model", default=os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL))
    parser.add_argument("--ollama-host", default=os.environ.get("OLLAMA_HOST"), help="default http://localhost:11434")
    parser.add_argument(
        "--llm-base-url",
        default=os.environ.get("LLM_BASE_URL"),
        help="call an OpenAI-format /chat/completions endpoint instead of Ollama's native API, "
        "e.g. a WSO2 AI gateway LLM provider (an API key can be set in LLM_API_KEY)",
    )
    parser.add_argument(
        "--llm-api-key-header",
        default=os.environ.get("LLM_API_KEY_HEADER", DEFAULT_API_KEY_HEADER),
        help="header that carries LLM_API_KEY: 'Authorization' sends 'Bearer <key>', any other "
        "header (e.g. API-Key for Agent Manager LLM proxies) sends the raw key",
    )
    parser.add_argument(
        "--member-id",
        default=os.environ.get("CONCIERGE_MEMBER_ID", DEFAULT_MEMBER_ID),
        help="Unity member id for the terminal chat (the API takes it from the forwarded user token)",
    )
    parser.add_argument(
        "--members",
        type=Path,
        default=Path(os.environ.get("CONCIERGE_MEMBERS", DEFAULT_MEMBERS)),
        help="JSON map of ThunderID username to Unity member id (with --serve)",
    )
    parser.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG, help="MCP servers JSON file")
    parser.add_argument(
        "--mcp-from-env",
        action="store_true",
        default=os.environ.get("MCP_FROM_ENV", "").lower() in ("1", "true", "yes"),
        help="read MCP servers from <PREFIX>_URL / <PREFIX>_API_KEY variables (as Agent Manager provides) "
        "instead of --config; also MCP_FROM_ENV=true",
    )
    parser.add_argument(
        "--mcp-env-prefixes",
        default=os.environ.get("MCP_ENV_PREFIXES", ",".join(DEFAULT_ENV_PREFIXES)),
        help="comma-separated variable prefixes for --mcp-from-env (default %(default)s)",
    )
    parser.add_argument("-p", "--prompt", help="ask one question and exit instead of starting a chat")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS, help="tool-calling rounds per question")
    parser.add_argument("-v", "--verbose", action="store_true", help="log MCP connection details")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="serve the agent HTTP API (for the UI, behind the Agent Manager gateway) instead of a terminal chat",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP API bind address (with --serve)")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)), help="HTTP port with --serve (or PORT)"
    )
    return parser.parse_args(argv)


def print_tool_call(name: str, arguments: Mapping[str, Any], result: str) -> None:
    """Show each tool call and a one-line preview of its result."""
    preview = result.replace("\n", " ")
    if len(preview) > 160:
        preview = preview[:157] + "..."
    print(f"  ↳ {name}({json.dumps(dict(arguments))}) → {preview}")


async def answer(agent: Agent, prompt: str, model: str) -> bool:
    """Print the agent's answer to `prompt`; return False if the turn failed."""
    try:
        print(f"agent> {await agent.ask(prompt)}")
    except ResponseError as exc:
        hint = (
            f" (is the model pulled? `ollama pull {model}`; via a gateway, check the URL)"
            if exc.status_code == 404
            else ""
        )
        print(f"error: the model endpoint rejected the request ({exc.status_code}): {exc.error}{hint}", file=sys.stderr)
        return False
    except ConnectionError as exc:
        print(f"error: cannot reach the model endpoint ({exc}); is Ollama / the gateway running?", file=sys.stderr)
        return False
    except AgentStepLimitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return False
    return True


async def chat_loop(agent: Agent, hub: McpHub, model: str) -> None:
    """Interactive REPL. `/tools` lists tools, `/reset` clears history, `exit` quits."""
    print(f"Model: {model}. Tools: {', '.join(hub.tool_names)}")
    print("Type /tools, /reset, or exit.")
    while True:
        try:
            prompt = (await asyncio.to_thread(input, "you> ")).strip()
        except EOFError:
            return
        if prompt.lower() in EXIT_COMMANDS:
            return
        if prompt == "/tools":
            print("\n".join(f"  {name}" for name in hub.tool_names))
        elif prompt == "/reset":
            agent.reset()
            print("History cleared.")
        elif prompt:
            await answer(agent, prompt, model)


@asynccontextmanager
async def open_llm(args: argparse.Namespace) -> AsyncIterator[ChatClient]:
    """Yield the chat client the flags ask for: OpenAI-format via `--llm-base-url`, else native Ollama."""
    if not args.llm_base_url:
        yield AsyncClient(host=args.ollama_host)
        return
    client = OpenAIChatClient(
        args.llm_base_url, api_key=os.environ.get("LLM_API_KEY"), api_key_header=args.llm_api_key_header
    )
    try:
        yield client
    finally:
        await client.aclose()


async def serve(
    args: argparse.Namespace, configs: list[ServerConfig], members: dict[str, str], identity: AgentIdentity | None
) -> int:
    """Serve the agent's HTTP API, sharing one MCP hub, LLM client and token broker across guests.

    Returns an exit code: 1 if the agent's own token (needed for the MCP handshake) can't be obtained.
    """
    async with httpx.AsyncClient(timeout=10) as http:
        broker = TokenBroker(identity, http) if identity else None
        try:
            startup_token = await broker.agent_token() if broker else None
        except TokenError as exc:
            print(f"error: cannot get the agent's token for the MCP handshake: {exc}", file=sys.stderr)
            return 1
        async with connect(configs, startup_token) as hub, open_llm(args) as llm:

            def new_conversation(guest: Guest, on_tool_call: ToolCallObserver) -> tuple[Agent, GuestTools]:
                tools = GuestTools(hub, broker)
                agent = Agent(
                    llm,
                    tools,
                    args.model,
                    system_prompt=build_system_prompt(guest.member_id, date.today()),
                    max_steps=args.max_steps,
                    on_tool_call=on_tool_call,
                )
                return agent, tools

            runtime = Runtime(model=args.model, tool_names=hub.tool_names, new_conversation=new_conversation)
            config = uvicorn.Config(
                create_app(runtime, members),
                host=args.host,
                port=args.port,
                log_level="info" if args.verbose else "warning",
            )
            tokens = (
                f"MCP calls carry JWTs: token exchange as {identity.client_id}"
                if identity
                else "no agent identity: MCP calls carry no token"
            )
            print(
                f"Concierge agent API on http://{args.host}:{args.port} (model {args.model}; {tokens}; "
                f"tools: {', '.join(hub.tool_names)})",
                flush=True,
            )
            await uvicorn.Server(config).serve()
    return 0


def _prefixes(raw: str) -> list[str]:
    return [prefix.strip() for prefix in raw.split(",") if prefix.strip()]


async def run(args: argparse.Namespace) -> int:
    """Connect to the MCP servers and run one prompt, the chat loop, or the HTTP API. Returns an exit code."""
    try:
        configs = load_env_servers(_prefixes(args.mcp_env_prefixes)) if args.mcp_from_env else load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.serve:
            try:
                members, identity = load_members(args.members), AgentIdentity.from_env(os.environ)
            except (MemberMapError, TokenConfigError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            return await serve(args, configs, members, identity)
        async with connect(configs) as hub, open_llm(args) as llm:
            agent = Agent(
                llm,
                hub,
                args.model,
                system_prompt=build_system_prompt(args.member_id, date.today()),
                max_steps=args.max_steps,
                on_tool_call=print_tool_call,
            )
            if args.prompt:
                return 0 if await answer(agent, args.prompt, args.model) else 1
            await chat_loop(agent, hub, args.model)
            return 0
    except McpConnectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    """Entry point for `python -m concierge`."""
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
