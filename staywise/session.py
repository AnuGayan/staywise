"""One signed-in guest's chat: their tokens, MCP connections, agent and event stream."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from staywise.agent import Agent, ChatClient, ToolCallHooks
from staywise.auth.broker import TokenBroker, UserGrant
from staywise.auth.policy import StepUpPolicy
from staywise.auth.stepup import StepUpCoordinator
from staywise.auth.thunder import ThunderClient
from staywise.config import ServerConfig
from staywise.events import CallContext, CallInfo, EventBus
from staywise.hub import McpHub, ToolResult, connect
from staywise.settings import Settings
from staywise.transport import StepUpTransport

logger = logging.getLogger(__name__)

RESULT_PREVIEW_CHARS = 600


@dataclass(frozen=True)
class Guest:
    """The signed-in user."""

    sub: str
    name: str


@dataclass(frozen=True)
class SessionDeps:
    """Process-wide services every session uses."""

    settings: Settings
    thunder: ThunderClient
    policy: StepUpPolicy
    llm: ChatClient
    model: str
    mcp_configs: Sequence[ServerConfig]
    inner_transport: httpx.AsyncBaseTransport | None = None  # tests route MCP traffic in memory


class TurnInProgressError(RuntimeError):
    """The guest sent a message while the previous one is still being answered."""


class ChatSession:
    """Owns the per-guest objects. MCP connections live in a task of their own because anyio
    requires them to be opened and closed by the same task, while turns run in other tasks."""

    def __init__(self, session_id: str, guest: Guest, grant: UserGrant, deps: SessionDeps) -> None:
        self.id = session_id
        self.guest = guest
        self._deps = deps
        self.bus = EventBus()
        self.calls = CallContext()
        self.stepups = StepUpCoordinator(
            self.bus.publish,
            deps.policy.user_stepup.timeout_seconds,
            start_path=f"{deps.settings.base_path}/auth/stepup/start",
        )
        self.broker = TokenBroker(
            deps.thunder,
            deps.policy,
            web_client=deps.settings.web_client,
            agent_client=deps.settings.agent_client,
            grant=grant,
            stepups=self.stepups,
            calls=self.calls,
            actor_scopes=deps.settings.actor_scopes,
            use_actor_token=deps.settings.use_actor_token,
        )
        self._agent: Agent | None = None
        self._hub_task: asyncio.Task[None] | None = None
        self._hub_ready = asyncio.Event()
        self._closing = asyncio.Event()
        self._hub: McpHub | None = None
        self._hub_error: str | None = None
        self._turn: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        """True while a turn is being answered."""
        return self._turn is not None and not self._turn.done()

    def start_turn(self, message: str) -> str:
        """Begin answering `message` in the background; progress arrives as events.

        Raises:
            TurnInProgressError: if a turn is already running.
        """
        if self.busy:
            raise TurnInProgressError("still answering the previous message")
        turn_id = uuid.uuid4().hex[:12]
        self._turn = asyncio.create_task(self._run_turn(turn_id, message), name=f"turn-{turn_id}")
        return turn_id

    def reset(self) -> None:
        """Forget the conversation (tokens and connections are kept).

        Raises:
            TurnInProgressError: if a turn is running.
        """
        if self.busy:
            raise TurnInProgressError("cannot reset while answering")
        if self._agent is not None:
            self._agent.reset()

    async def close(self) -> None:
        """Stop any turn, decline pending step-ups and close the MCP connections."""
        self.stepups.cancel_all()
        if self._turn is not None:
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._turn
        self._closing.set()
        if self._hub_task is not None:
            await self._hub_task

    async def _run_turn(self, turn_id: str, message: str) -> None:
        self.bus.publish("turn_started", {"turnId": turn_id, "message": message})
        try:
            agent = await self._get_agent()
            reply = await agent.ask(message, self._hooks(turn_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # report any failure to the UI instead of losing it in a background task
            logger.exception("turn %s failed", turn_id)
            self.bus.publish("turn_failed", {"turnId": turn_id, "error": _describe(exc)})
            return
        finally:
            self.calls.current = None
        self.bus.publish("assistant_message", {"turnId": turn_id, "content": reply})
        self.bus.publish("turn_finished", {"turnId": turn_id})

    def _hooks(self, turn_id: str) -> ToolCallHooks:
        def on_start(call_id: str, tool: str, server: str, arguments: dict[str, Any]) -> None:
            self.calls.current = CallInfo(turn_id, call_id, tool, server)
            self.bus.publish(
                "tool_call_started",
                {"turnId": turn_id, "callId": call_id, "tool": tool, "server": server, "arguments": arguments},
            )

        def on_end(call_id: str, result: ToolResult) -> None:
            self.calls.current = None
            event: dict[str, Any] = {
                "turnId": turn_id,
                "callId": call_id,
                "ok": result.ok,
                "resultPreview": result.text[:RESULT_PREVIEW_CHARS],
            }
            if not result.ok:
                event["error"] = result.text[:RESULT_PREVIEW_CHARS]
            self.bus.publish("tool_call_finished", event)

        return ToolCallHooks(on_start, on_end)

    async def _get_agent(self) -> Agent:
        if self._agent is not None:
            return self._agent
        if self._hub_task is None:
            self._hub_task = asyncio.create_task(self._hold_hub(), name=f"mcp-{self.id[:6]}")
        await self._hub_ready.wait()
        if self._hub is None:
            self._hub_task = None
            self._hub_ready.clear()
            raise RuntimeError(self._hub_error or "MCP servers are not connected")
        self._agent = Agent(self._deps.llm, self._hub, self._deps.model, guest_name=self.guest.name)
        return self._agent

    async def _hold_hub(self) -> None:
        try:
            async with connect(self._deps.mcp_configs, self._transport_for) as hub:
                self._hub = hub
                self._hub_ready.set()
                await self._closing.wait()
        except Exception as exc:  # surfaced to the guest by _get_agent
            logger.exception("MCP connection for session %s failed", self.id[:6])
            self._hub_error = _describe(exc)
        finally:
            self._hub = None
            self._hub_ready.set()

    def _transport_for(self, server: str) -> httpx.AsyncBaseTransport:
        return StepUpTransport(server, self.broker, self.calls, self.bus.publish, inner=self._deps.inner_transport)


def _describe(exc: BaseException) -> str:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return str(exc) or type(exc).__name__
