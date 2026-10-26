"""The concierge agent's HTTP API. The UI calls it with the guest's token; it serves no pages.

    POST /chat     {"message": "...", "conversation_id": "optional"} -> reply, tool calls, member id
    GET  /health   model and tool names

Requests arrive through the Agent Manager gateway, which validates the user token. The agent only reads
it: to know which Unity member it is serving, and to exchange it for MCP tokens (see `tokens.py`).
Each (guest, conversation_id) pair has its own history; turns within one conversation are serialized.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from json import JSONDecodeError
from typing import Any, Protocol

from ollama import ResponseError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from concierge.agent import Agent, AgentStepLimitError, ToolCallObserver
from concierge.identity import Guest, IdentityError, bearer_token, guest_from_token

DEFAULT_MAX_CONVERSATIONS = 100
DEFAULT_CONVERSATION_ID = "default"


class TokenHolder(Protocol):
    """Where a conversation keeps the guest's latest token for its tool calls (see `GuestTools`)."""

    user_token: str | None


ConversationFactory = Callable[[Guest, ToolCallObserver], tuple[Agent, TokenHolder]]


@dataclass(frozen=True)
class Runtime:
    """What the API needs from the running process: a way to start conversations, plus info for /health."""

    model: str
    tool_names: list[str]
    new_conversation: ConversationFactory


@dataclass
class Conversation:
    """One guest's chat: its agent, its token holder, a lock serializing turns, and this turn's tool calls."""

    agent: Agent
    tokens: TokenHolder
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class ConversationStore:
    """Keeps at most `max_conversations` conversations, evicting the least recently used."""

    def __init__(self, new_conversation: ConversationFactory, max_conversations: int = DEFAULT_MAX_CONVERSATIONS):
        self._new_conversation = new_conversation
        self._max = max_conversations
        self._conversations: OrderedDict[str, Conversation] = OrderedDict()

    def get_or_create(self, guest: Guest, conversation_id: str) -> Conversation:
        """The conversation for this guest and id; ids are scoped per guest, so guests can't share one."""
        key = f"{guest.subject}\0{conversation_id}"
        conversation = self._conversations.get(key)
        if conversation is None:
            conversation = self._create(guest)
            self._conversations[key] = conversation
            while len(self._conversations) > self._max:
                self._conversations.popitem(last=False)
        self._conversations.move_to_end(key)
        return conversation

    def __len__(self) -> int:
        return len(self._conversations)

    def _create(self, guest: Guest) -> Conversation:
        calls: list[dict[str, Any]] = []

        def record(name: str, arguments: Mapping[str, Any], result: str) -> None:
            calls.append({"name": name, "arguments": dict(arguments), "result": result})

        agent, tokens = self._new_conversation(guest, record)
        return Conversation(agent=agent, tokens=tokens, tool_calls=calls)


def create_app(
    runtime: Runtime, members: Mapping[str, str], max_conversations: int = DEFAULT_MAX_CONVERSATIONS
) -> Starlette:
    """Build the Starlette app serving one agent runtime."""
    conversations = ConversationStore(runtime.new_conversation, max_conversations)

    async def health(_: Request) -> Response:
        return JSONResponse(
            {"status": "ok", "model": runtime.model, "tools": runtime.tool_names, "conversations": len(conversations)}
        )

    async def chat(request: Request) -> Response:
        token = bearer_token(request.headers)
        if token is None:
            return _error(401, "no user token: send Authorization: Bearer <token>")
        try:
            guest = guest_from_token(token, members)
        except IdentityError as exc:
            return _error(exc.status, str(exc))
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return _error(415, "send the message as application/json")
        try:
            body = await request.json()
        except JSONDecodeError:
            return _error(400, "request body must be JSON")
        if not isinstance(body, dict):
            return _error(400, "request body must be a JSON object")
        message = body.get("message")
        conversation_id = body.get("conversation_id") or DEFAULT_CONVERSATION_ID
        if not isinstance(message, str) or not message.strip():
            return _error(400, "'message' must be a non-empty string")
        if not isinstance(conversation_id, str):
            return _error(400, "'conversation_id' must be a string")

        conversation = conversations.get_or_create(guest, conversation_id)
        async with conversation.lock:
            conversation.tokens.user_token = token  # the latest token, so tool calls never use a stale one
            conversation.tool_calls.clear()
            try:
                reply = await conversation.agent.ask(message)
            except ResponseError as exc:
                return _error(502, f"model endpoint rejected the request ({exc.status_code}): {exc.error}")
            except ConnectionError as exc:
                return _error(502, f"cannot reach the model endpoint: {exc}")
            except AgentStepLimitError as exc:
                return _error(500, str(exc))
            return JSONResponse(
                {"reply": reply, "tool_calls": list(conversation.tool_calls), "member_id": guest.member_id}
            )

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/chat", chat, methods=["POST"]),
        ]
    )


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)
