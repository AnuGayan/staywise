"""Run the StayWise agent backend.

    python -m staywise                                      # http://localhost:9100, Ollama on this machine
    python -m staywise -c config/mcp_servers.wso2.json \\
        --llm-base-url "$WSO2_GATEWAY_URL/ollama" --llm-api-key-header X-API-Key   # tools + model via the gateway

Thunder clients come from `.env.thunder` (written by wso2/thunder/thunder-setup.sh).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

import uvicorn
from ollama import AsyncClient

from staywise.agent import ChatClient
from staywise.auth.policy import PolicyError, load_policy
from staywise.auth.thunder import ThunderClient
from staywise.config import ConfigError, load_config
from staywise.envfile import load_env_files
from staywise.http_api import Backend, create_app
from staywise.openai_compat import DEFAULT_API_KEY_HEADER, OpenAIChatClient
from staywise.session import SessionDeps
from staywise.settings import Settings, SettingsError

DEFAULT_MODEL = "qwen3:1.7b"
DEFAULT_PORT = 9100


def build_parser() -> argparse.ArgumentParser:
    """CLI flags."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("-m", "--model", default=os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "-c", "--config", type=Path, help="MCP servers JSON (default: $MCP_CONFIG or config/mcp_servers.json)"
    )
    parser.add_argument(
        "--llm-base-url",
        default=os.environ.get("LLM_BASE_URL"),
        help="OpenAI-compatible endpoint, e.g. $WSO2_GATEWAY_URL/ollama (default: local Ollama)",
    )
    parser.add_argument("--llm-api-key-header", default=os.environ.get("LLM_API_KEY_HEADER", DEFAULT_API_KEY_HEADER))
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Start the server; returns a process exit code."""
    load_env_files()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = Settings.from_env()
        policy = load_policy(settings.policy_path)
        if acr := os.environ.get("STEPUP_ACR"):  # written by thunder-setup.sh
            policy = replace(policy, user_stepup=replace(policy.user_stepup, acr_values=acr))
        mcp_configs = load_config(args.config or settings.mcp_config)
    except (SettingsError, PolicyError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    llm: ChatClient
    if args.llm_base_url:
        llm = OpenAIChatClient(
            args.llm_base_url, api_key=os.environ.get("LLM_API_KEY"), api_key_header=args.llm_api_key_header
        )
    else:
        llm = AsyncClient()
    deps = SessionDeps(
        settings=settings,
        thunder=ThunderClient(settings.thunder_base, settings.resource, internal_url=settings.thunder_internal_url),
        policy=policy,
        llm=llm,
        model=args.model,
        mcp_configs=mcp_configs,
    )
    print(f"StayWise on {settings.public_url}  (model {args.model}, IdP {settings.thunder_base})", file=sys.stderr)
    uvicorn.run(create_app(Backend(deps)), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
