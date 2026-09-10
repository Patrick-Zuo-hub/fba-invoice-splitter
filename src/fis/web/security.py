from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable
from http.cookies import CookieError, SimpleCookie
from typing import Final
from urllib.parse import parse_qs, urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp

SESSION_COOKIE: Final = "fis_session"
CSRF_HEADER: Final = "x-csrf-token"
SESSION_MAX_AGE_SECONDS: Final = 12 * 60 * 60

_HOST_RE = re.compile(r"(?P<name>127\.0\.0\.1|localhost)(?::(?P<port>[0-9]{1,5}))?\Z")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class _RejectedRequest(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _raw_headers(request: Request, name: bytes) -> list[bytes]:
    return [value for key, value in request.scope["headers"] if key.lower() == name]


def _decode_ascii(value: bytes) -> str:
    try:
        return value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise _RejectedRequest(400) from exc


def _parse_loopback_authority(value: str) -> tuple[str, int]:
    match = _HOST_RE.fullmatch(value)
    if match is None:
        raise _RejectedRequest(400)
    port_text = match.group("port")
    port = 80 if port_text is None else int(port_text)
    if not 1 <= port <= 65535:
        raise _RejectedRequest(400)
    return match.group("name"), port


def _validated_host(request: Request) -> tuple[str, int]:
    values = _raw_headers(request, b"host")
    if len(values) != 1:
        raise _RejectedRequest(400)
    return _parse_loopback_authority(_decode_ascii(values[0]))


def _validated_origin(request: Request, host: tuple[str, int]) -> None:
    values = _raw_headers(request, b"origin")
    if len(values) != 1:
        raise _RejectedRequest(403)
    raw_origin = _decode_ascii(values[0])
    if not raw_origin or raw_origin != raw_origin.strip() or "," in raw_origin:
        raise _RejectedRequest(403)
    try:
        parsed = urlsplit(raw_origin)
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != ""
            or parsed.query != ""
            or parsed.fragment != ""
        ):
            raise _RejectedRequest(403)
        origin_authority = _parse_loopback_authority(parsed.netloc)
    except (ValueError, _RejectedRequest) as exc:
        raise _RejectedRequest(403) from exc
    if origin_authority != host:
        raise _RejectedRequest(403)


def _session_cookie(request: Request) -> str | None:
    raw_values = _raw_headers(request, b"cookie")
    if not raw_values:
        return None
    try:
        combined = "; ".join(_decode_ascii(value) for value in raw_values)
        cookie = SimpleCookie()
        cookie.load(combined)
    except (CookieError, _RejectedRequest):
        return None
    occurrences = sum(
        1
        for part in combined.split(";")
        if part.strip().partition("=")[0].strip() == SESSION_COOKIE
    )
    if occurrences != 1 or SESSION_COOKIE not in cookie:
        return None
    return cookie[SESSION_COOKIE].value


def _launch_candidate(request: Request) -> str | None:
    raw = request.scope.get("query_string", b"")
    try:
        query = _decode_ascii(raw)
    except _RejectedRequest:
        return None
    if not re.fullmatch(r"secret=[A-Za-z0-9_-]+", query):
        return None
    return query.removeprefix("secret=")


async def _csrf_candidates(request: Request) -> tuple[str | None, list[str]]:
    header_values = _raw_headers(request, CSRF_HEADER.encode("ascii"))
    if len(header_values) > 1:
        raise _RejectedRequest(403)
    header = _decode_ascii(header_values[0]) if header_values else None

    content_type = request.headers.get("content-type", "").lower()
    form_values: list[str] = []
    if content_type.startswith("application/x-www-form-urlencoded"):
        body = await request.body()
        try:
            parsed = parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=200,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise _RejectedRequest(403) from exc
        form_values = parsed.get("csrf_token", [])
    elif content_type.startswith("multipart/form-data"):
        await request.body()
        try:
            form = await request.form(max_fields=200, max_files=10)
        except Exception as exc:
            raise _RejectedRequest(403) from exc
        form_values = [str(value) for value in form.getlist("csrf_token")]
    return header, form_values


def _secure_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'"
    )
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


class LocalRequestMiddleware(BaseHTTPMiddleware):
    """Protect the loopback-only application with a per-process launch session."""

    def __init__(self, app: ASGIApp, *, launch_secret: str) -> None:
        super().__init__(app)
        if not isinstance(launch_secret, str) or not launch_secret:
            raise ValueError("launch_secret must be a non-empty string")
        self._launch_secret = launch_secret
        self._session_token = secrets.token_urlsafe(32)
        self._csrf_token = secrets.token_urlsafe(32)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        try:
            host = _validated_host(request)
            if request.url.path == "/health" and request.method in {"GET", "HEAD"}:
                response = await call_next(request)
            elif request.url.path == "/launch" and request.method == "GET":
                response = self._exchange_launch_secret(request)
            else:
                self._require_session(request)
                request.state.csrf_token = self._csrf_token
                if request.method not in _SAFE_METHODS:
                    _validated_origin(request, host)
                    await self._require_csrf(request)
                response = await call_next(request)
        except _RejectedRequest as exc:
            message = "请求无效" if exc.status_code == 400 else "禁止访问"
            response = PlainTextResponse(message, status_code=exc.status_code)
        return _secure_headers(response)

    def _exchange_launch_secret(self, request: Request) -> Response:
        candidate = _launch_candidate(request)
        if candidate is None or not secrets.compare_digest(candidate, self._launch_secret):
            raise _RejectedRequest(403)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            self._session_token,
            max_age=SESSION_MAX_AGE_SECONDS,
            path="/",
            secure=False,
            httponly=True,
            samesite="strict",
        )
        return response

    def _require_session(self, request: Request) -> None:
        candidate = _session_cookie(request)
        if candidate is None or not secrets.compare_digest(candidate, self._session_token):
            raise _RejectedRequest(401)

    async def _require_csrf(self, request: Request) -> None:
        header, form_values = await _csrf_candidates(request)
        if len(form_values) > 1:
            raise _RejectedRequest(403)
        candidates = [value for value in (header, *form_values) if value is not None]
        if not candidates or any(
            not secrets.compare_digest(candidate, self._csrf_token) for candidate in candidates
        ):
            raise _RejectedRequest(403)
