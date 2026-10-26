"""Web chat and HTTP API for the concierge. Guests sign in with the Agent Manager ThunderID first.

    GET    /                 the chat page (redirects to /login when not signed in)
    GET    /login            start an OIDC sign-in at ThunderID
    GET    /auth/callback    finish the sign-in, set the session cookie
    POST   /logout           end the session
    GET    /me               the signed-in guest
    GET    /health           model and tool names (public)
    POST   /chat             {"message": "..."} -> reply + tool calls     (signed in)
    DELETE /chat             start a fresh conversation                  (signed in)

Each signed-in session has its own conversation, bound to that guest's Unity member id; requests
within one session are serialized.
"""

from __future__ import annotations

import asyncio
import html
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from json import JSONDecodeError
from pathlib import Path
from typing import Any

import httpx
from ollama import ResponseError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from concierge.agent import Agent, AgentStepLimitError, ToolCallObserver
from concierge.auth import AuthError, SignedInGuest, WebAuth

DEFAULT_MAX_SESSIONS = 100
CHAT_PAGE = Path(__file__).resolve().parent / "static" / "index.html"
SESSION_COOKIE = "hr_concierge_session"

AgentFactory = Callable[[SignedInGuest, ToolCallObserver], Agent]


@dataclass(frozen=True)
class Runtime:
    """What the API needs from the running process: a way to build agents, plus info for /health."""

    model: str
    tool_names: list[str]
    new_agent: AgentFactory


@dataclass
class Conversation:
    """One guest's chat: its agent, a lock serializing its turns, and the current turn's tool calls."""

    agent: Agent
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class ConversationStore:
    """Keeps at most `max_sessions` conversations keyed by sign-in session, evicting the least recently used."""

    def __init__(self, new_agent: AgentFactory, max_sessions: int = DEFAULT_MAX_SESSIONS):
        self._new_agent = new_agent
        self._max_sessions = max_sessions
        self._conversations: OrderedDict[str, Conversation] = OrderedDict()

    def get_or_create(self, session_id: str, guest: SignedInGuest) -> Conversation:
        """Return the conversation for `session_id`, creating one for `guest` if needed."""
        conversation = self._conversations.get(session_id)
        if conversation is None:
            conversation = self._create(guest)
            self._conversations[session_id] = conversation
            while len(self._conversations) > self._max_sessions:
                self._conversations.popitem(last=False)
        self._conversations.move_to_end(session_id)
        return conversation

    def drop(self, session_id: str) -> None:
        """Forget a conversation (a no-op for unknown ids)."""
        self._conversations.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._conversations)

    def _create(self, guest: SignedInGuest) -> Conversation:
        calls: list[dict[str, Any]] = []

        def record(name: str, arguments: Mapping[str, Any], result: str) -> None:
            calls.append({"name": name, "arguments": dict(arguments), "result": result})

        return Conversation(agent=self._new_agent(guest, record), tool_calls=calls)


def create_app(
    runtime: Runtime,
    auth: WebAuth,
    *,
    secure_cookies: bool = False,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
) -> Starlette:
    """Build the Starlette app serving one agent runtime behind ThunderID sign-in."""
    conversations = ConversationStore(runtime.new_agent, max_sessions)

    def signed_in(request: Request) -> tuple[str, SignedInGuest] | None:
        session_id = request.cookies.get(SESSION_COOKIE)
        guest = auth.guest(session_id)
        if session_id is None or guest is None:
            return None
        return session_id, guest

    async def chat_page(request: Request) -> Response:
        if signed_in(request) is None:
            return RedirectResponse("/login", status_code=303)
        return FileResponse(CHAT_PAGE, media_type="text/html")

    async def login(_: Request) -> Response:
        try:
            return RedirectResponse(await auth.begin_login(), status_code=303)
        except httpx.HTTPError as exc:
            return _sign_in_failed(f"cannot reach ThunderID ({exc})", status=502)

    async def callback(request: Request) -> Response:
        params = request.query_params
        if "error" in params:
            detail = params.get("error_description") or params["error"]
            return _sign_in_failed(f"ThunderID reported: {detail}")
        state, code = params.get("state"), params.get("code")
        if not state or not code:
            return _sign_in_failed("the sign-in response is missing its code or state")
        try:
            session_id, ttl = await auth.complete_login(state, code)
        except AuthError as exc:
            return _sign_in_failed(str(exc))
        except httpx.HTTPError as exc:
            return _sign_in_failed(f"cannot reach ThunderID ({exc})", status=502)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE, session_id, max_age=ttl, httponly=True, samesite="lax", secure=secure_cookies
        )
        return response

    async def logout(request: Request) -> Response:
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id:
            conversations.drop(session_id)
        auth.logout(session_id)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="lax", secure=secure_cookies)
        return response

    async def me(request: Request) -> Response:
        current = signed_in(request)
        if current is None:
            return _error(401, "sign in first")
        _, guest = current
        return JSONResponse({"username": guest.username, "name": guest.name, "member_id": guest.member_id})

    async def health(_: Request) -> Response:
        return JSONResponse(
            {"status": "ok", "model": runtime.model, "tools": runtime.tool_names, "sessions": len(conversations)}
        )

    async def chat(request: Request) -> Response:
        current = signed_in(request)
        if current is None:
            return _error(401, "sign in first")
        # A JSON body cannot be sent cross-site without a CORS preflight, which this app never allows;
        # together with the SameSite=Lax cookie this keeps other sites from chatting as the guest.
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return _error(415, "send the message as application/json")
        try:
            body = await request.json()
        except JSONDecodeError:
            return _error(400, "request body must be JSON")
        if not isinstance(body, dict):
            return _error(400, "request body must be a JSON object")
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            return _error(400, "'message' must be a non-empty string")

        session_id, guest = current
        conversation = conversations.get_or_create(session_id, guest)
        async with conversation.lock:
            conversation.tool_calls.clear()
            try:
                reply = await conversation.agent.ask(message)
            except ResponseError as exc:
                return _error(502, f"model endpoint rejected the request ({exc.status_code}): {exc.error}")
            except ConnectionError as exc:
                return _error(502, f"cannot reach the model endpoint: {exc}")
            except AgentStepLimitError as exc:
                return _error(500, str(exc))
            return JSONResponse({"reply": reply, "tool_calls": list(conversation.tool_calls)})

    async def reset(request: Request) -> Response:
        current = signed_in(request)
        if current is None:
            return _error(401, "sign in first")
        conversations.drop(current[0])
        return Response(status_code=204)

    return Starlette(
        routes=[
            Route("/", chat_page, methods=["GET"]),
            Route("/login", login, methods=["GET"]),
            Route("/auth/callback", callback, methods=["GET"]),
            Route("/logout", logout, methods=["POST"]),
            Route("/me", me, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/chat", chat, methods=["POST"]),
            Route("/chat", reset, methods=["DELETE"]),
        ]
    )


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _sign_in_failed(reason: str, status: int = 400) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><title>Sign-in failed</title>"
        f"<p>Sign-in failed: {html.escape(reason)}</p><p><a href='/login'>Try again</a></p>",
        status_code=status,
    )
