from pathlib import Path

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from fis.config import Settings
from fis.web.app import create_app
from fis.web.security import LocalRequestPolicy

HOST = "127.0.0.1:8765"
ORIGIN = f"http://{HOST}"


def _client(tmp_path: Path) -> TestClient:
    app = create_app(Settings(data_root=tmp_path / "data"), bound_port=8765)

    @app.api_route("/_test/mutation", methods=["POST", "PUT", "PATCH", "DELETE"])
    def test_mutation() -> Response:
        return Response(status_code=204)

    return TestClient(app)


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
        " 127.0.0.1:8765",
        "127.0.0.1:8765 ",
    ],
)
def test_host_must_exactly_match_bound_authority(tmp_path: Path, host: str) -> None:
    response = _client(tmp_path).get("/health", headers={"host": host})

    assert response.status_code == 400
    assert response.text == "请求无效"
    assert host not in response.text


def test_missing_duplicate_and_non_ascii_host_are_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.get("/health", headers={}).status_code == 400
    assert (
        client.get("/health", headers=[("host", HOST), ("Host", HOST)]).status_code
        == 400
    )
    assert client.get("/health", headers=[(b"host", b"\xff")]).status_code == 400


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
