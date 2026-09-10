import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from fis.config import Settings
from fis.web import security
from fis.web.app import create_app
from fis.web.security import LocalRequestPolicy, SanitizedErrorMiddleware

HOST = "127.0.0.1:8765"
ORIGIN = f"http://{HOST}"
SECURITY_HEADER_NAMES = {
    b"cache-control",
    b"content-security-policy",
    b"cross-origin-resource-policy",
    b"permissions-policy",
    b"referrer-policy",
    b"x-content-type-options",
    b"x-frame-options",
}


def _client(tmp_path: Path) -> TestClient:
    app = create_app(Settings(data_root=tmp_path / "data"), bound_port=8765)

    @app.api_route("/_test/mutation", methods=["POST", "PUT", "PATCH", "DELETE"])
    def test_mutation() -> Response:
        return Response(status_code=204)

    return TestClient(app)


@contextmanager
def _running_uvicorn(
    tmp_path: Path,
    configure: Callable[[FastAPI], None] | None = None,
) -> Iterator[int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    app = create_app(Settings(data_root=tmp_path / "data"), bound_port=port)
    if configure is not None:
        configure(app)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            access_log=False,
            lifespan="off",
            log_config=None,
            server_header=False,
            date_header=False,
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise RuntimeError("test Uvicorn server did not start")
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("test Uvicorn server did not stop")


def _raw_http(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.settimeout(2)
        connection.sendall(request)
        chunks: list[bytes] = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.parametrize("port", [0, 65536, -1, True, False, 1.5, "8765"])
def test_policy_rejects_invalid_bound_port(port: object) -> None:
    with pytest.raises(ValueError, match="bound_port"):
        LocalRequestPolicy(port)  # type: ignore[arg-type]


def test_policy_is_immutable_and_uses_exact_loopback_values() -> None:
    policy = LocalRequestPolicy(8765)

    assert policy.authority == HOST
    assert policy.origin == ORIGIN
    with pytest.raises((AttributeError, TypeError)):
        policy.bound_port = 9000  # type: ignore[misc]


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example:8765",
        "localhost:8765",
        "127.0.0.1",
        "127.0.0.1:8766",
        "127.0.0.1.:8765",
        "user@127.0.0.1:8765",
        "[::1]:8765",
        "127.0.0.1:8765,attacker.example",
    ],
)
def test_host_must_exactly_match_bound_authority(tmp_path: Path, host: str) -> None:
    response = _client(tmp_path).get("/health", headers={"host": host})

    assert response.status_code == 400
    assert response.text == "请求无效"
    assert host not in response.text


def test_nonmatching_duplicate_and_non_ascii_asgi_host_are_rejected(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)

    assert client.get("/health", headers={}).status_code == 400
    assert (
        client.get("/health", headers=[("host", HOST), ("Host", HOST)]).status_code
        == 400
    )
    assert client.get("/health", headers=[(b"host", b"\xff")]).status_code == 400


def test_real_server_defines_host_ows_and_malformed_request_boundary(
    tmp_path: Path,
) -> None:
    with _running_uvicorn(tmp_path) as port:
        wrong_port = 1 if port == 65535 else port + 1
        cases = [
            (f"Host:\t 127.0.0.1:{port} \t\r\n".encode("ascii"), 200),
            (b"", 400),
            (
                f"Host: 127.0.0.1:{port}\r\nHost: 127.0.0.1:{port}\r\n".encode(
                    "ascii"
                ),
                400,
            ),
            (f"Host : 127.0.0.1:{port}\r\n".encode("ascii"), 400),
            (f"Host: localhost:{port}\r\n".encode("ascii"), 400),
            (f"Host: 127.0.0.1:{wrong_port}\r\n".encode("ascii"), 400),
            (f"Host: 127.0.0.1.:{port}\r\n".encode("ascii"), 400),
            (f"Host: 127.0.0.1:{port},attacker.example\r\n".encode("ascii"), 400),
        ]
        for host_line, expected_status in cases:
            response = _raw_http(
                port,
                b"GET /health HTTP/1.1\r\n"
                + host_line
                + b"Connection: close\r\n\r\n",
            )
            assert response.startswith(f"HTTP/1.1 {expected_status} ".encode("ascii"))


def test_real_server_sanitizes_unhandled_error_response_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def add_error_route(app: FastAPI) -> None:
        @app.get("/_test/error")
        def error_route() -> None:
            raise RuntimeError("sensitive-customer-value")

    caplog.set_level(logging.ERROR)
    with _running_uvicorn(tmp_path, add_error_route) as port:
        response = _raw_http(
            port,
            (
                "GET /_test/error HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii"),
        )

    head, body = response.split(b"\r\n\r\n", 1)
    response_header_names = {
        line.split(b":", 1)[0].lower() for line in head.split(b"\r\n")[1:]
    }
    assert response.startswith(b"HTTP/1.1 500 ")
    assert body == "内部服务器错误".encode()
    assert SECURITY_HEADER_NAMES <= response_header_names
    assert "FIS_WEB_UNHANDLED" in caplog.text
    assert "sensitive-customer-value" not in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.asyncio
async def test_started_response_error_is_sanitized_without_duplicate_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent: list[Message] = []

    async def failing_app(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise RuntimeError("sensitive-stream-value")

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8765),
    }
    caplog.set_level(logging.ERROR)

    middleware = SanitizedErrorMiddleware(failing_app)
    await middleware(scope, receive, send)

    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert len(starts) == 1
    assert starts[0]["status"] == 200
    assert SECURITY_HEADER_NAMES <= {
        name.lower() for name, _value in starts[0]["headers"]
    }
    assert "FIS_WEB_UNHANDLED" in caplog.text
    assert "sensitive-stream-value" not in caplog.text
    assert "Traceback" not in caplog.text


def test_sanitizer_wraps_local_boundary_failures(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_boundary(_request: object) -> None:
        raise RuntimeError("sensitive-boundary-value")

    monkeypatch.setattr(security, "_reject_forwarding_headers", fail_boundary)
    caplog.set_level(logging.ERROR)

    response = TestClient(
        create_app(Settings(data_root=tmp_path / "data"), bound_port=8765),
        raise_server_exceptions=False,
    ).get("/health", headers={"host": HOST})

    assert response.status_code == 500
    assert response.text == "内部服务器错误"
    assert "FIS_WEB_UNHANDLED" in caplog.text
    assert "sensitive-boundary-value" not in caplog.text
    for name in SECURITY_HEADER_NAMES:
        assert name.decode("ascii") in response.headers


@pytest.mark.parametrize(
    "headers",
    [
        [("forwarded", "for=127.0.0.1")],
        [("Forwarded", "for=127.0.0.1"), ("forwarded", "for=127.0.0.1")],
        [("x-forwarded-for", "127.0.0.1")],
        [("X-Forwarded-Host", HOST)],
        [("x-forwarded-proto", "http")],
        [("x-forwarded-port", "8765")],
        [("x-forwarded-anything", "value")],
    ],
)
def test_forwarding_headers_are_rejected(
    tmp_path: Path, headers: list[tuple[str, str]]
) -> None:
    response = _client(tmp_path).get("/health", headers=[("host", HOST), *headers])

    assert response.status_code == 400
    assert response.text == "请求无效"


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_unsafe_methods_accept_only_exact_origin_and_request_marker(
    tmp_path: Path, method: str
) -> None:
    response = getattr(_client(tmp_path), method)(
        "/_test/mutation",
        headers={"host": HOST, "origin": ORIGIN, "x-fis-request": "1"},
    )

    assert response.status_code == 204


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "",
        "HTTP://127.0.0.1:8765",
        "https://127.0.0.1:8765",
        "http://localhost:8765",
        "http://127.0.0.1",
        "http://127.0.0.1:8766",
        "http://127.0.0.1:8765/",
        "http://127.0.0.1:8765, http://attacker.example",
        " http://127.0.0.1:8765",
        "http://127.0.0.1:8765 ",
        "null",
    ],
)
def test_unsafe_method_rejects_missing_or_non_exact_origin(
    tmp_path: Path, origin: str | None
) -> None:
    headers = {"host": HOST, "x-fis-request": "1"}
    if origin is not None:
        headers["origin"] = origin

    response = _client(tmp_path).post("/_test/mutation", headers=headers)

    assert response.status_code == 403
    assert response.text == "禁止访问"
    if origin:
        assert origin not in response.text


def test_unsafe_method_rejects_duplicate_origin(tmp_path: Path) -> None:
    response = _client(tmp_path).post(
        "/_test/mutation",
        headers=[
            ("host", HOST),
            ("origin", ORIGIN),
            ("Origin", ORIGIN),
            ("x-fis-request", "1"),
        ],
    )

    assert response.status_code == 403


def test_unsafe_method_rejects_non_ascii_origin(tmp_path: Path) -> None:
    response = _client(tmp_path).post(
        "/_test/mutation",
        headers=[
            (b"host", HOST.encode("ascii")),
            (b"origin", b"\xff"),
            (b"x-fis-request", b"1"),
        ],
    )

    assert response.status_code == 403


@pytest.mark.parametrize("marker", [None, "", "0", "01", "true", "ONE", " 1", "1 ", "1,1"])
def test_unsafe_method_rejects_missing_or_non_exact_request_marker(
    tmp_path: Path, marker: str | None
) -> None:
    headers = {"host": HOST, "origin": ORIGIN}
    if marker is not None:
        headers["x-fis-request"] = marker

    response = _client(tmp_path).post("/_test/mutation", headers=headers)

    assert response.status_code == 403
    assert response.text == "禁止访问"


def test_unsafe_method_rejects_duplicate_request_marker(tmp_path: Path) -> None:
    response = _client(tmp_path).post(
        "/_test/mutation",
        headers=[
            ("host", HOST),
            ("origin", ORIGIN),
            ("x-fis-request", "1"),
            ("X-FIS-Request", "1"),
        ],
    )

    assert response.status_code == 403


def test_get_and_head_do_not_require_unsafe_headers(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.get("/", headers={"host": HOST}).status_code == 200
    assert client.head("/", headers={"host": HOST}).status_code == 200


def test_get_cannot_invoke_post_only_mutation_route(tmp_path: Path) -> None:
    assert _client(tmp_path).get(
        "/_test/mutation", headers={"host": HOST}
    ).status_code == 405


def test_options_does_not_grant_cors_access(tmp_path: Path) -> None:
    response = _client(tmp_path).options(
        "/_test/mutation",
        headers={
            "host": HOST,
            "origin": "http://attacker.example",
            "access-control-request-method": "POST",
        },
    )

    assert response.status_code == 405
    assert not any(name.lower().startswith("access-control-allow-") for name in response.headers)
