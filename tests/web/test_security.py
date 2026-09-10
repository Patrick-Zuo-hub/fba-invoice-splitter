from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from fis.config import Settings
from fis.web.app import create_app

HOST = "127.0.0.1:8765"
ORIGIN = f"http://{HOST}"


def _client(tmp_path: Path) -> TestClient:
    app = create_app(Settings(data_root=tmp_path / "data"), launch_secret="secret")

    @app.post("/test-mutation", status_code=204)
    def test_mutation() -> Response:
        return Response(status_code=204)

    return TestClient(app)


def _launch(client: TestClient, *, host: str = HOST) -> str:
    response = client.get(
        "/launch?secret=secret", headers={"host": host}, follow_redirects=False
    )
    assert response.status_code == 303
    page = client.get("/", headers={"host": host})
    marker = 'content="'
    tail = page.text.split('name="csrf-token"', 1)[1]
    return tail.split(marker, 1)[1].split('"', 1)[0]


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example",
        "localhost.attacker.example",
        "127.0.0.1.attacker.example",
        "localhost.",
        "localhost:0",
        "localhost:65536",
        "localhost:abc",
        "user@localhost",
        "localhost,attacker.example",
        " localhost",
        "localhost ",
        "[::1]",
        "[::1]:8765",
    ],
)
def test_unexpected_or_ambiguous_host_is_rejected(tmp_path: Path, host: str) -> None:
    client = _client(tmp_path)

    assert client.get("/health", headers={"host": host}).status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", HOST, "localhost:65535"])
def test_exact_loopback_hosts_are_allowed(tmp_path: Path, host: str) -> None:
    client = _client(tmp_path)

    assert client.get("/health", headers={"host": host}).status_code == 200


def test_duplicate_host_headers_are_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get(
        "/health",
        headers=[("host", "127.0.0.1"), ("host", "localhost")],
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "path",
    [
        "/launch",
        "/launch?secret=",
        "/launch?secret=wrong",
        "/launch?secret=secret&secret=secret",
        "/launch?secret=secret&next=/",
        "/launch?secret=secret%0d%0aX-Test:bad",
    ],
)
def test_launch_requires_one_exact_secret_without_reflection(tmp_path: Path, path: str) -> None:
    client = _client(tmp_path)

    response = client.get(path, headers={"host": HOST}, follow_redirects=False)

    assert response.status_code == 403
    assert "secret" not in response.text.lower()
    assert "wrong" not in response.text


def test_launch_cookie_is_scoped_for_loopback_http(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get(
        "/launch?secret=secret", headers={"host": HOST}, follow_redirects=False
    )

    cookie = response.headers["set-cookie"]
    assert "fis_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=" in cookie
    assert "Secure" not in cookie
    assert "secret" not in cookie


def test_wrong_or_missing_session_cannot_access_application(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.get("/", headers={"host": HOST}).status_code == 401
    client.cookies.set("fis_session", "wrong")
    assert client.get("/", headers={"host": HOST}).status_code == 401


def test_launch_url_does_not_survive_the_redirect_target(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/launch?secret=secret", headers={"host": HOST})

    assert response.status_code == 200
    assert urlsplit(str(response.url)).path == "/"
    assert urlsplit(str(response.url)).query == ""
    assert "secret" not in response.history[0].headers["location"]


def test_get_routes_do_not_require_origin_or_csrf(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _launch(client)

    assert client.get("/", headers={"host": HOST}).status_code == 200


def test_health_mutation_does_not_bypass_the_session(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.post("/health", headers={"host": HOST}).status_code == 401


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:8765",
        "http://localhost:8765",
        "http://127.0.0.1",
        "http://127.0.0.1:8766",
        "http://user@127.0.0.1:8765",
        "http://127.0.0.1:8765/extra",
        "http://127.0.0.1:8765?x=1",
        "http://127.0.0.1:8765#x",
        "http://127.0.0.1:8765, http://attacker.example",
        "null",
    ],
)
def test_mutation_rejects_missing_or_non_matching_origin(tmp_path: Path, origin: str) -> None:
    client = _client(tmp_path)
    token = _launch(client)

    response = client.post(
        "/test-mutation",
        headers={"host": HOST, "origin": origin, "x-csrf-token": token},
    )

    assert response.status_code == 403


def test_mutation_rejects_missing_origin(tmp_path: Path) -> None:
    client = _client(tmp_path)
    token = _launch(client)

    response = client.post(
        "/test-mutation", headers={"host": HOST, "x-csrf-token": token}
    )
    assert response.status_code == 403


@pytest.mark.parametrize("token", [None, "", "wrong"])
def test_mutation_rejects_missing_or_wrong_csrf_header(tmp_path: Path, token: str | None) -> None:
    client = _client(tmp_path)
    _launch(client)
    headers = {"host": HOST, "origin": ORIGIN}
    if token is not None:
        headers["x-csrf-token"] = token

    assert client.post("/test-mutation", headers=headers).status_code == 403


def test_mutation_accepts_csrf_header(tmp_path: Path) -> None:
    client = _client(tmp_path)
    token = _launch(client)

    response = client.post(
        "/test-mutation",
        headers={"host": HOST, "origin": ORIGIN, "x-csrf-token": token},
    )

    assert response.status_code == 204


def test_mutation_accepts_urlencoded_csrf_form_field(tmp_path: Path) -> None:
    client = _client(tmp_path)
    token = _launch(client)

    response = client.post(
        "/test-mutation",
        headers={"host": HOST, "origin": ORIGIN},
        data={"csrf_token": token},
    )

    assert response.status_code == 204


def test_mutation_accepts_multipart_csrf_form_field(tmp_path: Path) -> None:
    client = _client(tmp_path)
    token = _launch(client)

    response = client.post(
        "/test-mutation",
        headers={"host": HOST, "origin": ORIGIN},
        data={"csrf_token": token},
        files={"invoice": ("invoice.xlsx", b"placeholder", "application/octet-stream")},
    )

    assert response.status_code == 204


def test_mutation_rejects_conflicting_header_and_form_tokens(tmp_path: Path) -> None:
    client = _client(tmp_path)
    token = _launch(client)

    response = client.post(
        "/test-mutation",
        headers={"host": HOST, "origin": ORIGIN, "x-csrf-token": token},
        data={"csrf_token": "wrong"},
    )

    assert response.status_code == 403
