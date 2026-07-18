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
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from . import curator
from .api import api_error, cancel_job, create_job, get_job, job_events, project_detail, projects
from .jobs import JobHandler, JobRunner
from .project_store import ProjectStore


_CSP = "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
_SESSION_COOKIE = "ai_sprite_studio_session"
# Mutating curator routes serve the vendored (token-less) UI, so they rely on
# loopback + same-origin instead of a CSRF token for cross-site defense.
_ORIGIN_GUARDED = frozenset({"/api/curation", "/curator/upload", "/curator/normalize"})


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


def _secure(response: Response) -> Response:
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
            await _secure(api_error("invalid_host", "Local access is required", status_code=400))(
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
                await _secure(
                    api_error("invalid_origin", "Same-origin local access is required", status_code=403)
                )(scope, receive, send)
                return
            session_id = Request(scope).cookies.get(_SESSION_COOKIE)
            expected = scope["app"].state.sessions.get(session_id or "")
            provided = headers.get("x-csrf-token")
            if not expected or not provided or not secrets.compare_digest(expected, provided):
                await _secure(
                    api_error("csrf_failed", "A valid CSRF token is required", status_code=403)
                )(scope, receive, send)
                return
        elif scope["path"] in _ORIGIN_GUARDED and scope["method"] == "POST":
            if not _same_loopback_origin(headers.get("origin"), host):
                await _secure(
                    api_error("invalid_origin", "Same-origin local access is required", status_code=403)
                )(scope, receive, send)
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
        "<p>Upload one image, a sprite sheet, or several per-frame images, then curate.</p>"
        "<form id=\"upload\">"
        "<p><label>Image(s) <input type=\"file\" name=\"file\" multiple "
        "accept=\"image/png,image/jpeg,image/webp\" required></label> "
        "<small>pick multiple = one image per frame</small></p>"
        "<p><label><input type=\"checkbox\" name=\"autosplit\"> Sprite sheet &mdash; auto-detect frames</label> "
        "<small>recommended for a sheet: finds frames by background gaps, ignores empty rows/margins</small></p>"
        "<p><label>Frames <input type=\"number\" name=\"frames\" value=\"4\" "
        "min=\"1\" max=\"12\"></label> <small>ignored for multiple files / auto-detect</small></p>"
        "<p><label>Grid cols <input type=\"number\" name=\"cols\" value=\"0\" min=\"0\" max=\"12\" style=\"width:4em\"></label> "
        "<label>rows <input type=\"number\" name=\"rows\" value=\"0\" min=\"0\" max=\"12\" style=\"width:4em\"></label> "
        "<small>manual grid fallback; leave 0 if using auto-detect</small></p>"
        "<p><label>Segmentation <select name=\"segmentation\">"
        "<option value=\"components\">components</option>"
        "<option value=\"projection\">projection</option></select></label></p>"
        "<p><button type=\"submit\">Upload &amp; extract</button></p></form>"
        "<p id=\"status\" role=\"status\"></p>"
        "<p><a href=\"/curator\">Open curator</a> (after uploading)</p>"
        "<script src=\"/curator/upload.js\"></script>"
        "</main></body></html>"
    )
    if request.cookies.get(_SESSION_COOKIE) != session_id:
        response.set_cookie(_SESSION_COOKIE, session_id, httponly=True, samesite="strict")
    return response


async def _http_error(request, exc: HTTPException) -> Response:
    codes = {404: "not_found", 405: "method_not_allowed"}
    return _secure(
        api_error(
            codes.get(exc.status_code, "http_error"),
            "The request cannot be completed",
            status_code=exc.status_code,
        )
    )


async def _unexpected_error(request, _exc: Exception) -> Response:
    return _secure(api_error("internal_error", "The request cannot be completed", status_code=500))


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
            Route("/curator/upload.js", curator.upload_js),
            Route("/curator/upload", curator.upload, methods=["POST"]),
            Route("/curator/suite.js", curator.suite_js),
            Route("/curator/normalize", curator.normalize, methods=["POST"]),
            Route("/curator", curator.curator_index),
            Route("/curator.js", curator.curator_asset),
            Route("/curator.css", curator.curator_asset),
            Route("/curator/frame/{artifact_id}", curator.frame_bytes),
            Route("/api/run", curator.api_run),
            Route("/api/progress", curator.api_progress),
            Route("/api/curation", curator.api_curation, methods=["POST"]),
            Route("/run/{name}", curator.run_file),
            Route("/download/{kind}", curator.download),
        ],
        middleware=[Middleware(LocalSecurityMiddleware)],
        lifespan=lifespan,
        exception_handlers={HTTPException: _http_error, Exception: _unexpected_error},
    )
    app.state.store = ProjectStore(workspace)
    app.state.runner = JobRunner(app.state.store, handler)
    app.state.sessions = {}
    app.state.curator = {"project_id": None}
    return app
