"""The guest chat UI: signs the guest in with ThunderID and relays chat to the agent with their token.

    GET    /                 the chat page (redirects to /login when signed out)
    GET    /login            start an OIDC sign-in at ThunderID
    GET    /auth/callback    finish the sign-in, set the session cookie
    POST   /logout           end the session
    GET    /me               the signed-in user
    GET    /health           where the agent is (public)
    POST   /chat             {"message": "..."} -> forwarded to the agent as the guest     (signed in)
    DELETE /chat             start a new conversation                                     (signed in)

The browser only holds an HttpOnly session cookie; the user token stays here and goes to the agent as
`Authorization: Bearer ...`. The Agent Manager gateway in front of the agent validates it.
"""

from __future__ import annotations

import html
import uuid
from collections.abc import Mapping
from json import JSONDecodeError
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from concierge.auth import AuthError, SignedInUser, WebAuth

CHAT_PAGE = Path(__file__).resolve().parent / "static" / "index.html"
SESSION_COOKIE = "hr_concierge_session"
AGENT_TIMEOUT_SECONDS = 180


def create_app(
    auth: WebAuth,
    agent: httpx.AsyncClient,
    *,
    agent_headers: Mapping[str, str] | None = None,
    secure_cookies: bool = False,
    base_path: str = "",
) -> Starlette:
    """Build the UI app. `agent` is a client whose base URL is the agent's (gateway) URL."""
    conversation_ids: dict[str, str] = {}
    extra_headers = dict(agent_headers or {})

    def signed_in(request: Request) -> tuple[str, SignedInUser] | None:
        session_id = request.cookies.get(SESSION_COOKIE)
        user = auth.user(session_id)
        if session_id is None or user is None:
            return None
        return session_id, user

    async def chat_page(request: Request) -> Response:
        if signed_in(request) is None:
            return RedirectResponse(f"{base_path}/login", status_code=303)
        return FileResponse(CHAT_PAGE, media_type="text/html")

    async def login(_: Request) -> Response:
        try:
            return RedirectResponse(await auth.begin_login(), status_code=303)
        except httpx.HTTPError as exc:
            return _sign_in_failed(f"cannot reach ThunderID ({exc})", base_path, status=502)

    async def callback(request: Request) -> Response:
        params = request.query_params
        if "error" in params:
            detail = params.get("error_description") or params["error"]
            return _sign_in_failed(f"ThunderID reported: {detail}", base_path)
        state, code = params.get("state"), params.get("code")
        if not state or not code:
            return _sign_in_failed("the sign-in response is missing its code or state", base_path)
        try:
            session_id, ttl = await auth.complete_login(state, code)
        except AuthError as exc:
            return _sign_in_failed(str(exc), base_path)
        except httpx.HTTPError as exc:
            return _sign_in_failed(f"cannot reach ThunderID ({exc})", base_path, status=502)
        response = RedirectResponse(f"{base_path}/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE, session_id, max_age=ttl, httponly=True, samesite="lax", secure=secure_cookies
        )
        return response

    async def logout(request: Request) -> Response:
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id:
            conversation_ids.pop(session_id, None)
        auth.logout(session_id)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="lax", secure=secure_cookies)
        return response

    async def me(request: Request) -> Response:
        current = signed_in(request)
        if current is None:
            return _error(401, "sign in first")
        _, user = current
        return JSONResponse({"username": user.username, "name": user.name})

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok", "agent": str(agent.base_url)})

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
        message = body.get("message") if isinstance(body, dict) else None
        if not isinstance(message, str) or not message.strip():
            return _error(400, "'message' must be a non-empty string")

        session_id, user = current
        conversation_id = conversation_ids.setdefault(session_id, uuid.uuid4().hex)
        try:
            reply = await agent.post(
                "chat",
                json={"message": message, "conversation_id": conversation_id},
                headers={**extra_headers, "Authorization": f"Bearer {user.access_token}"},
                timeout=AGENT_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            return _error(502, f"cannot reach the agent: {exc}")
        if reply.status_code == httpx.codes.UNAUTHORIZED:
            # The gateway (or agent) no longer accepts this token: make the guest sign in again.
            auth.logout(session_id)
            return _error(401, "your sign-in has expired; please sign in again")
        try:
            payload = reply.json()
        except ValueError:
            return _error(502, f"the agent answered HTTP {reply.status_code}: {reply.text[:200]}")
        return JSONResponse(payload, status_code=reply.status_code)

    async def new_conversation(request: Request) -> Response:
        current = signed_in(request)
        if current is None:
            return _error(401, "sign in first")
        conversation_ids[current[0]] = uuid.uuid4().hex
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
            Route("/chat", new_conversation, methods=["DELETE"]),
        ]
    )


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _sign_in_failed(reason: str, base_path: str, status: int = 400) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><title>Sign-in failed</title>"
        f"<p>Sign-in failed: {html.escape(reason)}</p>"
        f"<p><a href='{html.escape(base_path)}/login'>Try again</a></p>",
        status_code=status,
    )
