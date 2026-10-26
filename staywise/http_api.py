"""The agent's web backend (a backend-for-frontend: tokens stay here, the browser gets a session cookie).

Routes are relative to PUBLIC_URL's path, which a proxy strips (Agent Manager serves the agent under
/<routePath>); every URL handed to the browser carries that prefix.

GET  /auth/login                  → Thunder sign-in (authorization code + PKCE)
GET  /auth/callback               ← Thunder; creates the chat session
POST /auth/logout
GET  /auth/stepup/start/{id}      → Thunder MFA (step-up app, acr_values, prompt=login); opened in a popup
GET  /auth/stepup/callback        ← Thunder; resumes the paused tool call
GET  /api/me                      who is signed in, and the scopes they consented to
POST /api/chat                    {"message"} → 202 {"turnId"}; the answer streams over /api/events
POST /api/reset
GET  /api/events                  Server-Sent Events (see staywise/events.py and web/src/api/types.ts);
                                  a reconnect with Last-Event-ID replays what it missed
POST /api/stepup/{id}/cancel      the guest declined
GET  /api/mock-phone/messages
POST /mock-phone/sms              Thunder's custom SMS sender (needs X-Mock-Phone-Key)
GET  /health
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from staywise.auth.broker import UserGrant
from staywise.auth.stepup import PendingStepUp
from staywise.auth.thunder import OAuthError, Pkce
from staywise.mock_phone import PhoneInbox, PhoneMessage
from staywise.session import ChatSession, Guest, SessionDeps, TurnInProgressError

logger = logging.getLogger(__name__)

SESSION_COOKIE = "staywise_sid"
LOGIN_STATE_TTL_SECONDS = 600
MAX_SESSIONS = 50
MAX_MESSAGE_CHARS = 4000
SSE_PING_SECONDS = 15
SSE_RETRY_MS = 1000


@dataclass
class LoginState:
    """A sign-in in progress."""

    nonce: str
    pkce: Pkce
    created: float = field(default_factory=time.time)


class Backend:
    """Route handlers plus the in-memory session and login stores."""

    def __init__(self, deps: SessionDeps, inbox: PhoneInbox | None = None) -> None:
        self.deps = deps
        self.inbox = inbox or PhoneInbox()
        self.inbox.set_listener(self._broadcast_sms)
        self.sessions: OrderedDict[str, ChatSession] = OrderedDict()
        self._logins: dict[str, LoginState] = {}
        base_path = deps.settings.base_path
        self._login_url = f"{base_path}/auth/login"
        self._cookie_path = base_path or "/"

    # ---- sign-in ------------------------------------------------------------------------

    async def login(self, _request: Request) -> Response:
        self._expire_logins()
        state, login = secrets.token_urlsafe(24), LoginState(secrets.token_urlsafe(24), Pkce.new())
        self._logins[state] = login
        settings = self.deps.settings
        url = self.deps.thunder.authorization_url(
            settings.web_client.client_id,
            settings.login_redirect_uri,
            " ".join(sorted(self.deps.policy.login_scopes)),
            state=state,
            nonce=login.nonce,
            pkce=login.pkce,
        )
        return RedirectResponse(url, status_code=302)

    async def callback(self, request: Request) -> Response:
        params = request.query_params
        login = self._logins.pop(params.get("state", ""), None)
        if login is None or time.time() - login.created > LOGIN_STATE_TTL_SECONDS:
            return _page("Sign-in expired", "Please start again.", status=400, link=(self._login_url, "Sign in"))
        if "error" in params:
            return _page("Sign-in failed", params.get("error_description") or params["error"], status=400)
        settings = self.deps.settings
        try:
            tokens = await self.deps.thunder.exchange_code(
                settings.web_client, params.get("code", ""), settings.login_redirect_uri, login.pkce.verifier
            )
            identity = await self.deps.thunder.verify_id_token(
                tokens.id_token or "", settings.web_client.client_id, login.nonce
            )
        except OAuthError as exc:
            logger.warning("sign-in failed: %s", exc)
            return _page("Sign-in failed", exc.description or exc.error, status=400)

        guest = Guest(sub=str(identity["sub"]), name=_display_name(identity))
        session_id = secrets.token_urlsafe(32)
        await self._store(ChatSession(session_id, guest, UserGrant(guest.sub, tokens), self.deps))
        response = RedirectResponse(f"{settings.ui_url}/", status_code=302)
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            path=self._cookie_path,
            httponly=True,
            samesite="lax",
            secure=settings.public_url.startswith("https"),
        )
        return response

    async def logout(self, request: Request) -> Response:
        session = self.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        if session is not None:
            await session.close()
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path=self._cookie_path)
        return response

    # ---- step-up ------------------------------------------------------------------------

    async def stepup_start(self, request: Request) -> Response:
        session = self._session(request)
        stepup = session.stepups.get(request.path_params["stepup_id"]) if session else None
        if stepup is None:
            return _page("Verification expired", "Ask StayWise to try again.", status=404)
        stepup.started = True
        settings, policy = self.deps.settings, self.deps.policy
        url = self.deps.thunder.authorization_url(
            settings.stepup_client.client_id,
            settings.stepup_redirect_uri,
            " ".join(sorted(policy.user_stepup.scopes)),
            state=stepup.state,
            nonce=stepup.nonce,
            pkce=stepup.pkce,
            acr_values=policy.user_stepup.acr_values,
            prompt="login",
        )
        return RedirectResponse(url, status_code=302)

    async def stepup_callback(self, request: Request) -> Response:
        params = request.query_params
        session = self._session(request)
        stepup = session.stepups.by_state(params.get("state", "")) if session else None
        if session is None or stepup is None:
            return _closing_page("Verification expired", "This request is no longer waiting.")
        if "error" in params:
            session.stepups.decline(stepup)
            return _closing_page("Verification cancelled", "StayWise will not complete that action.")
        problem = await self._complete_stepup(session, stepup, params.get("code", ""))
        if problem:
            session.stepups.decline(stepup, f"verification failed: {problem}")
            return _closing_page("Verification failed", problem)
        return _closing_page("Verified", "You can close this window; StayWise is continuing.")

    async def _complete_stepup(self, session: ChatSession, stepup: PendingStepUp, code: str) -> str | None:
        """Redeem the code and check it is the same person with the expected assurance; None if all good."""
        settings = self.deps.settings
        try:
            tokens = await self.deps.thunder.exchange_code(
                settings.stepup_client, code, settings.stepup_redirect_uri, stepup.pkce.verifier
            )
            identity = await self.deps.thunder.verify_id_token(
                tokens.id_token or "", settings.stepup_client.client_id, stepup.nonce
            )
        except OAuthError as exc:
            logger.warning("step-up token request failed: %s", exc)
            return exc.description or exc.error
        if identity.get("sub") != session.guest.sub or tokens.claims.get("sub") != session.guest.sub:
            return "a different account signed in"
        expected_acr = self.deps.policy.user_stepup.acr_values.split()
        if "acr" in identity and identity["acr"] not in expected_acr:
            return f"sign-in assurance {identity['acr']!r} is not {' or '.join(expected_acr)}"
        session.stepups.approve(stepup, tokens)
        return None

    async def stepup_cancel(self, request: Request) -> Response:
        session = self._session(request)
        if session is None:
            return self._unauthorized()
        stepup = session.stepups.get(request.path_params["stepup_id"])
        if stepup is None:
            return JSONResponse({"error": "no such pending verification"}, status_code=404)
        session.stepups.decline(stepup)
        return Response(status_code=204)

    # ---- chat ---------------------------------------------------------------------------

    async def me(self, request: Request) -> Response:
        session = self._session(request)
        if session is None:
            return self._unauthorized()
        return JSONResponse(
            {
                "authenticated": True,
                "user": {"sub": session.guest.sub, "name": session.guest.name},
                "grantedScopes": sorted(session.broker.granted_scopes()),
            }
        )

    async def chat(self, request: Request) -> Response:
        session = self._session(request)
        if session is None:
            return self._unauthorized()
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        message = body.get("message") if isinstance(body, dict) else None
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_CHARS:
            return JSONResponse({"error": f"message must be 1-{MAX_MESSAGE_CHARS} characters"}, status_code=400)
        try:
            turn_id = session.start_turn(message.strip())
        except TurnInProgressError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"turnId": turn_id}, status_code=202)

    async def reset(self, request: Request) -> Response:
        session = self._session(request)
        if session is None:
            return self._unauthorized()
        try:
            session.reset()
        except TurnInProgressError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return Response(status_code=204)

    async def events(self, request: Request) -> Response:
        session = self._session(request)
        if session is None:
            return self._unauthorized()
        queue = session.bus.subscribe(after=_last_event_id(request))

        async def stream() -> AsyncIterator[ServerSentEvent]:
            try:
                yield ServerSentEvent(retry=SSE_RETRY_MS)
                while True:
                    event = await queue.get()
                    yield ServerSentEvent(json.dumps(event.data), event=event.type, id=str(event.id))
            finally:
                session.bus.unsubscribe(queue)

        return EventSourceResponse(stream(), ping=SSE_PING_SECONDS)

    # ---- mock phone ---------------------------------------------------------------------

    async def phone_messages(self, request: Request) -> Response:
        if self._session(request) is None:
            return self._unauthorized()
        return JSONResponse([message.to_json() for message in self.inbox.messages()])

    async def receive_sms(self, request: Request) -> Response:
        key = request.headers.get("x-mock-phone-key", "")
        if not hmac.compare_digest(key.encode(), self.deps.settings.mock_phone_key.encode()):
            return JSONResponse({"error": "bad key"}, status_code=401)
        self.inbox.receive(await request.body(), request.headers.get("content-type", ""))
        return JSONResponse({"status": "delivered"})

    def _broadcast_sms(self, message: PhoneMessage) -> None:
        for session in self.sessions.values():
            session.bus.publish("phone_message", message.to_json())

    async def health(self, _request: Request) -> Response:
        return JSONResponse({"status": "ok", "model": self.deps.model, "sessions": len(self.sessions)})

    # ---- helpers ------------------------------------------------------------------------

    def _unauthorized(self) -> Response:
        return JSONResponse({"authenticated": False, "loginUrl": self._login_url}, status_code=401)

    def _session(self, request: Request) -> ChatSession | None:
        session_id = request.cookies.get(SESSION_COOKIE, "")
        session = self.sessions.get(session_id)
        if session is not None:
            self.sessions.move_to_end(session_id)
        return session

    async def _store(self, session: ChatSession) -> None:
        self.sessions[session.id] = session
        while len(self.sessions) > MAX_SESSIONS:
            _, oldest = self.sessions.popitem(last=False)
            await oldest.close()

    def _expire_logins(self) -> None:
        cutoff = time.time() - LOGIN_STATE_TTL_SECONDS
        for state in [s for s, login in self._logins.items() if login.created < cutoff]:
            del self._logins[state]

    async def close(self) -> None:
        """Close every session (on shutdown)."""
        sessions = list(self.sessions.values())
        self.sessions.clear()
        await asyncio.gather(*(session.close() for session in sessions))


def create_app(backend: Backend) -> Starlette:
    """The ASGI app. The built web UI is served at `/` when `web/dist` exists."""
    routes: list[Route | Mount] = [
        Route("/auth/login", backend.login),
        Route("/auth/callback", backend.callback),
        Route("/auth/logout", backend.logout, methods=["POST"]),
        Route("/auth/stepup/start/{stepup_id}", backend.stepup_start),
        Route("/auth/stepup/callback", backend.stepup_callback),
        Route("/api/me", backend.me),
        Route("/api/chat", backend.chat, methods=["POST"]),
        Route("/api/reset", backend.reset, methods=["POST"]),
        Route("/api/events", backend.events),
        Route("/api/stepup/{stepup_id}/cancel", backend.stepup_cancel, methods=["POST"]),
        Route("/api/mock-phone/messages", backend.phone_messages),
        Route("/mock-phone/sms", backend.receive_sms, methods=["POST"]),
        Route("/health", backend.health),
    ]
    dist = backend.deps.settings.web_dist
    if dist.is_dir():
        routes.append(Mount("/", app=StaticFiles(directory=dist, html=True), name="web"))

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        yield
        await backend.close()

    return Starlette(routes=routes, lifespan=lifespan)


def _display_name(identity: dict[str, Any]) -> str:
    for key in ("name", "given_name", "preferred_username", "username", "email"):
        if identity.get(key):
            return str(identity[key])
    return "Guest"


def _last_event_id(request: Request) -> int | None:
    value = request.headers.get("last-event-id", "")
    return int(value) if value.isdigit() else None


def _page(title: str, message: str, *, status: int = 200, link: tuple[str, str] | None = None) -> HTMLResponse:
    anchor = f'<p><a href="{html.escape(link[0])}">{html.escape(link[1])}</a></p>' if link else ""
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
        f"<body style='font-family:system-ui;padding:2rem'><h1>{html.escape(title)}</h1>"
        f"<p>{html.escape(message)}</p>{anchor}</body>",
        status_code=status,
    )


def _closing_page(title: str, message: str) -> HTMLResponse:
    """Shown in the step-up popup; closes itself. The main window learns the outcome over SSE."""
    page = _page(title, message)
    body = bytes(page.body).replace(b"</body>", b"<script>setTimeout(() => window.close(), 1200)</script></body>")
    return HTMLResponse(body)
