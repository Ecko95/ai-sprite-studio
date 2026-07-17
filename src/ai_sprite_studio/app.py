"""Loopback-only Starlette application shell."""

from __future__ import annotations

from contextlib import asynccontextmanager
from html import escape
import ipaddress
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .api import api_error, cancel_job, create_job, get_job, job_events, project_detail, projects
from .jobs import JobHandler, JobRunner
from .project_store import ProjectStore


_CSP = "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
_SESSION_COOKIE = "ai_sprite_studio_session"


def _is_loopback_host(value: str | None) -> bool:
    if not value or "," in value:
        return False
    try:
        parsed = urlsplit(f"//{value}")
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    if not host or parsed.username or parsed.password:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_and_port(value: str | None) -> tuple[str, int] | None:
    if not value or "," in value:
        return None
    try:
        parsed = urlsplit(f"//{value}")
        host = parsed.hostname
        port = parsed.port or 80
    except ValueError:
        return None
    if not host or parsed.username or parsed.password:
        return None
    return host.lower(), port


def _same_loopback_origin(origin: str | None, host_header: str | None) -> bool:
    host = _host_and_port(host_header)
    if host is None or not origin:
        return False
    try:
        parsed = urlsplit(origin)
        port = parsed.port or 80
    except ValueError:
        return False
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not _is_loopback_host(parsed.netloc)
    ):
        return False
    return (parsed.hostname.lower(), port) == host


def _error(code: str, message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"code": code, "message": message, "retryable": False, "details": {}},
        status_code=status_code,
    )


def _secure(response: JSONResponse | HTMLResponse) -> JSONResponse | HTMLResponse:
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


class LocalSecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host")
        if not _is_loopback_host(host):
            await _secure(_error("invalid_host", "Local access is required", status_code=400))(
                scope, receive, send
            )
            return
        if scope["path"].startswith("/api/v1/") and scope["method"] in {
            "POST",
            "PATCH",
            "PUT",
            "DELETE",
        }:
            if not _same_loopback_origin(headers.get("origin"), host):
                await _secure(_error("invalid_origin", "Same-origin local access is required", status_code=403))(
                    scope, receive, send
                )
                return
            session_id = Request(scope).cookies.get(_SESSION_COOKIE)
            expected = scope["app"].state.sessions.get(session_id or "")
            provided = headers.get("x-csrf-token")
            if not expected or not provided or not secrets.compare_digest(expected, provided):
                await _secure(_error("csrf_failed", "A valid CSRF token is required", status_code=403))(
                    scope, receive, send
                )
                return

        async def send_securely(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                headers["Content-Security-Policy"] = _CSP
                headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        await self.app(scope, receive, send_securely)


async def _root(request):
    sessions: dict[str, str] = request.app.state.sessions
    session_id = request.cookies.get(_SESSION_COOKIE)
    csrf_token = sessions.get(session_id or "")
    if csrf_token is None:
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        sessions[session_id] = csrf_token
    response = HTMLResponse(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"csrf-token\" content=\"{escape(csrf_token)}\">"
        "<title>AI Sprite Studio</title></head><body><main><h1>AI Sprite Studio</h1>"
        "<p>Local app ready.</p></main></body></html>"
    )
    if request.cookies.get(_SESSION_COOKIE) != session_id:
        response.set_cookie(_SESSION_COOKIE, session_id, httponly=True, samesite="strict")
    return response


async def _http_error(request, exc: HTTPException) -> JSONResponse:
    codes = {404: "not_found", 405: "method_not_allowed"}
    return api_error(codes.get(exc.status_code, "http_error"), "The request cannot be completed", status_code=exc.status_code)


async def _unexpected_error(request, _exc: Exception) -> JSONResponse:
    return api_error("internal_error", "The request cannot be completed", status_code=500)


def create_app(workspace: str | Path, handler: JobHandler | None = None) -> Starlette:
    @asynccontextmanager
    async def lifespan(app):
        await app.state.runner.start()
        try:
            yield
        finally:
            await app.state.runner.aclose()

    app = Starlette(
        routes=[
            Route("/", _root),
            Route("/api/v1/projects", projects, methods=["GET", "POST"]),
            Route(
                "/api/v1/projects/{project_id}",
                project_detail,
                methods=["GET", "PATCH"],
            ),
            Route("/api/v1/projects/{project_id}/jobs", create_job, methods=["POST"]),
            Route("/api/v1/jobs/{job_id}", get_job),
            Route("/api/v1/jobs/{job_id}/events", job_events),
            Route("/api/v1/jobs/{job_id}/cancel", cancel_job, methods=["POST"]),
        ],
        middleware=[Middleware(LocalSecurityMiddleware)],
        lifespan=lifespan,
        exception_handlers={HTTPException: _http_error, Exception: _unexpected_error},
    )
    app.state.store = ProjectStore(workspace)
    app.state.runner = JobRunner(app.state.store, handler)
    app.state.sessions = {}
    return app
