from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LocalRequestPolicy:
    """Exact request boundary for one loopback listener."""

    bound_port: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.bound_port, bool)
            or not isinstance(self.bound_port, int)
            or not 1 <= self.bound_port <= 65535
        ):
            raise ValueError("bound_port must be an integer from 1 to 65535")

    @property
    def authority(self) -> str:
        return f"127.0.0.1:{self.bound_port}"

    @property
    def origin(self) -> str:
        return f"http://{self.authority}"


class _RejectedRequest(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _raw_headers(request: Request, name: bytes) -> list[bytes]:
    return [value for key, value in request.scope["headers"] if key.lower() == name]


def _reject_forwarding_headers(request: Request) -> None:
    for name, _value in request.scope["headers"]:
        lowered = name.lower()
        if lowered == b"forwarded" or lowered.startswith(b"x-forwarded-"):
            raise _RejectedRequest(400)


def _require_exact_header(
    request: Request,
    *,
    name: bytes,
    expected: str,
    status_code: int,
) -> None:
    values = _raw_headers(request, name)
    if len(values) != 1 or values[0] != expected.encode("ascii"):
        raise _RejectedRequest(status_code)


class SanitizedErrorMiddleware:
    """Keep unhandled HTTP failures out of both responses and server logs."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        response_started = False

        async def send_secured(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        try:
            await self._app(scope, receive, send_secured)
        except Exception:  # noqa: BLE001 - BaseException must reach the server runtime.
            _LOGGER.error("FIS_WEB_UNHANDLED")
            if response_started:
                return
            response = PlainTextResponse("内部服务器错误", status_code=500)
            await response(scope, receive, send_secured)


class LocalRequestMiddleware(BaseHTTPMiddleware):
    """Reject requests that do not exactly match the local listener boundary."""

    def __init__(self, app: ASGIApp, *, policy: LocalRequestPolicy) -> None:
        super().__init__(app)
        self._policy = policy

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        try:
            _reject_forwarding_headers(request)
            _require_exact_header(
                request,
                name=b"host",
                expected=self._policy.authority,
                status_code=400,
            )
            if request.method in _UNSAFE_METHODS:
                _require_exact_header(
                    request,
                    name=b"origin",
                    expected=self._policy.origin,
                    status_code=403,
                )
                _require_exact_header(
                    request,
                    name=b"x-fis-request",
                    expected="1",
                    status_code=403,
                )
            response = await call_next(request)
        except _RejectedRequest as exc:
            message = "请求无效" if exc.status_code == 400 else "禁止访问"
            response = PlainTextResponse(message, status_code=exc.status_code)
        return response
