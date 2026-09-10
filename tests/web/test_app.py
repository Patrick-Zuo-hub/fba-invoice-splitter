from pathlib import Path

from fastapi.testclient import TestClient

from fis.config import Settings
from fis.web.app import create_app


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(Settings(data_root=tmp_path / "data"), launch_secret="secret"))


def _launch(client: TestClient) -> None:
    response = client.get(
        "/launch?secret=secret",
        headers={"host": "127.0.0.1:8765"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_health_is_the_only_unauthenticated_data_free_route(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/health", headers={"host": "127.0.0.1"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/", headers={"host": "127.0.0.1"}).status_code == 401
    assert client.get("/missing", headers={"host": "127.0.0.1"}).status_code == 401


def test_launch_redirects_to_clean_root_and_renders_minimal_page(tmp_path: Path) -> None:
    client = _client(tmp_path)

    launch = client.get(
        "/launch?secret=secret",
        headers={"host": "localhost:8123"},
        follow_redirects=False,
    )

    assert launch.status_code == 303
    assert launch.headers["location"] == "/"
    assert "secret" not in launch.text
    assert "secret" not in launch.headers["location"]
    page = client.get("/", headers={"host": "localhost:8123"})
    assert page.status_code == 200
    assert "FBA Invoice Splitter" in page.text
    assert 'name="csrf-token"' in page.text
    assert "secret" not in page.text


def test_static_content_is_protected_by_the_session(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.get("/static/app.css", headers={"host": "localhost"}).status_code == 401

    _launch(client)
    response = client.get("/static/app.css", headers={"host": "127.0.0.1:8765"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def test_framework_documentation_endpoints_are_disabled(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _launch(client)

    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path, headers={"host": "127.0.0.1:8765"}).status_code == 404


def test_security_headers_are_applied_to_success_and_rejection(tmp_path: Path) -> None:
    client = _client(tmp_path)

    for response in (
        client.get("/health", headers={"host": "localhost"}),
        client.get("/health", headers={"host": "attacker.example"}),
    ):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-security-policy"].startswith("default-src 'self'")
