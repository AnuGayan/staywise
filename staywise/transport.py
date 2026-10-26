"""httpx transport that authorizes MCP requests and performs step-up on 401/403.

It sits *under* the MCP SDK's HTTP client instead of being an `httpx.Auth`, because the SDK
(1.23.0) calls `raise_for_status()` on a POST inside its task group: one final 403 would tear
down the whole MCP connection. Here a refused step-up becomes an ordinary JSON-RPC tool error
the model can read and explain, and the connection stays up.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from staywise.auth.broker import IssuedToken, TokenBroker
from staywise.auth.challenge import parse_challenge
from staywise.auth.stepup import StepUpRefusedError
from staywise.events import CallContext, EventSink

logger = logging.getLogger(__name__)

MAX_CHALLENGES_PER_REQUEST = 2
DISPLAY_CLAIMS = ("sub", "client_id", "scope", "aud", "exp", "iat", "jti", "act")


@dataclass(frozen=True)
class RpcInfo:
    """The parts of a JSON-RPC request body the transport needs."""

    id: Any
    method: str | None
    tool: str | None

    @property
    def is_request(self) -> bool:
        """Requests carry an id; notifications do not and get no response body."""
        return self.id is not None


def rpc_info(request: httpx.Request) -> RpcInfo:
    """Read the JSON-RPC id, method and tool name from a request (empty for non-JSON bodies)."""
    try:
        body = json.loads(request.content) if request.content else {}
    except ValueError:
        return RpcInfo(None, None, None)
    if not isinstance(body, dict):
        return RpcInfo(None, None, None)
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    tool = params.get("name") if body.get("method") == "tools/call" else None
    return RpcInfo(body.get("id"), body.get("method"), tool)


class StepUpTransport(httpx.AsyncBaseTransport):
    """One per MCP server per chat session."""

    def __init__(
        self,
        server: str,
        broker: TokenBroker,
        calls: CallContext,
        emit: EventSink,
        inner: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._server = server
        self._broker = broker
        self._calls = calls
        self._emit = emit
        self._owns_inner = inner is None
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._clock = clock

    async def aclose(self) -> None:
        if self._owns_inner:
            await self._inner.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        rpc = rpc_info(request)
        started = self._clock()
        try:
            token = await self._broker.token_for(self._server)
            response = await self._send(request, token)
            for _ in range(MAX_CHALLENGES_PER_REQUEST):
                if response.status_code not in (401, 403) or request.method != "POST":
                    break
                challenge = parse_challenge(response.status_code, response.headers.get("www-authenticate"))
                await response.aclose()
                logger.info("%s %s: %s %s", self._server, rpc.tool or rpc.method, challenge.status, challenge.error)
                token = await self._broker.resolve(self._server, token, challenge)
                response = await self._send(request, token)
        except StepUpRefusedError as exc:
            return self._refusal(rpc, str(exc))
        if response.status_code in (401, 403):
            await response.aclose()
            return self._refusal(rpc, "the server still refused the request after step-up")
        if rpc.tool:
            self._announce(token, fresh=token.minted_at >= started)
        return response

    async def _send(self, request: httpx.Request, token: IssuedToken) -> httpx.Response:
        request.headers["Authorization"] = f"Bearer {token.value}"
        return await self._inner.handle_async_request(request)

    def _announce(self, token: IssuedToken, *, fresh: bool) -> None:
        call = self._calls.current
        if call is None:
            return
        self._emit(
            "token_issued",
            {
                "turnId": call.turn_id,
                "callId": call.call_id,
                "strategy": token.strategy if fresh else "cached",
                "scopes": sorted(token.scopes),
                "claims": {key: token.claims[key] for key in DISPLAY_CLAIMS if key in token.claims},
            },
        )

    def _refusal(self, rpc: RpcInfo, reason: str) -> httpx.Response:
        """A well-formed MCP answer saying the call was not authorized."""
        if not rpc.is_request:
            return httpx.Response(202)
        if rpc.tool:
            text = (
                f"Error: not authorized to run {rpc.tool}: {reason}. "
                "Do not retry this action unless the guest asks again."
            )
            body: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": rpc.id,
                "result": {"content": [{"type": "text", "text": text}], "isError": True},
            }
        else:
            body = {"jsonrpc": "2.0", "id": rpc.id, "error": {"code": -32001, "message": f"unauthorized: {reason}"}}
        return httpx.Response(200, json=body)
