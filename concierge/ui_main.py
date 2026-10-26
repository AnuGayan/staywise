"""Run the guest chat UI (`hardrock-concierge-ui`): ThunderID sign-in, then chat relayed to the agent."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping

import httpx
import uvicorn

from concierge.auth import AuthConfigError, OidcClient, OidcSettings, WebAuth
from concierge.ui_app import create_app

DEFAULT_PORT = 9100
DEFAULT_AGENT_URL = "http://localhost:8000"
DEFAULT_AGENT_API_KEY_HEADER = "X-API-Key"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse flags; AGENT_URL, AGENT_API_KEY(_HEADER) and UI_PORT provide defaults."""
    parser = argparse.ArgumentParser(prog="hardrock-concierge-ui", description="Hard Rock Miami concierge chat UI.")
    parser.add_argument(
        "--agent-url",
        default=os.environ.get("AGENT_URL", DEFAULT_AGENT_URL),
        help="the agent's URL, e.g. its Agent Manager endpoint (default %(default)s)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("UI_PORT", DEFAULT_PORT)))
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def agent_headers(environ: Mapping[str, str]) -> dict[str, str]:
    """Optional static header for the agent endpoint, e.g. an Agent Manager API key while one is required."""
    key = environ.get("AGENT_API_KEY", "").strip()
    if not key:
        return {}
    return {environ.get("AGENT_API_KEY_HEADER", DEFAULT_AGENT_API_KEY_HEADER): key}


async def serve(args: argparse.Namespace, oidc: OidcSettings) -> None:
    """Serve the UI until interrupted."""
    agent_url = args.agent_url.rstrip("/") + "/"
    async with (
        httpx.AsyncClient(timeout=10) as idp_http,
        httpx.AsyncClient(base_url=agent_url) as agent_http,
    ):
        app = create_app(
            WebAuth(OidcClient(oidc, idp_http)),
            agent_http,
            agent_headers=agent_headers(os.environ),
            secure_cookies=oidc.secure_cookies,
            base_path=oidc.base_path,
        )
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info" if args.verbose else "warning")
        print(f"Concierge UI on {oidc.public_url} (sign-in: {oidc.issuer}; agent: {agent_url})", flush=True)
        await uvicorn.Server(config).serve()


def main() -> None:
    """Entry point for `hardrock-concierge-ui`."""
    args = parse_args()
    try:
        oidc = OidcSettings.from_env(os.environ)
    except AuthConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        asyncio.run(serve(args, oidc))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
