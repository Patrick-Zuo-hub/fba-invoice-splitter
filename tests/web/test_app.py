from pathlib import Path

from fastapi import Response
from fastapi.testclient import TestClient

from fis.config import Settings
from fis.web.app import create_app

HOST = "127.0.0.1:8765"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(Settings(data_root=tmp_path / "data"), bound_port=8765))


def _assert_security_headers(response: Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'"
    )
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["permissions-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert not any(
        name.lower().startswith("access-control-allow-") for name in response.headers
    )


def test_app_records_settings_and_actual_bound_port(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "data")
    app = create_app(settings, bound_port=8765)

    assert app.state.settings is settings
    assert app.state.bound_port == 8765


def test_root_is_directly_available_without_cookie_or_tokens(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/", headers={"host": HOST})

    assert response.status_code == 200
    assert "FBA 发票拆分工具" in response.text
    assert "csrf" not in response.text.lower()
    assert "launch" not in response.text.lower()
    assert "session" not in response.text.lower()
    assert "set-cookie" not in response.headers


def test_launch_route_is_removed(tmp_path: Path) -> None:
    response = _client(tmp_path).get(
        "/launch?secret=anything", headers={"host": HOST}, follow_redirects=False
    )

    assert response.status_code == 404
    assert "set-cookie" not in response.headers


def test_health_returns_only_ok_status(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/health", headers={"host": HOST})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-fis-product"] == "fba-invoice-splitter"


def test_static_content_is_available_with_exact_host(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/static/app.css", headers={"host": HOST})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def test_framework_documentation_endpoints_are_disabled(tmp_path: Path) -> None:
    client = _client(tmp_path)

    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path, headers={"host": HOST}).status_code == 404


def test_security_headers_are_applied_to_success_rejection_and_not_found(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)

    for response in (
        client.get("/health", headers={"host": HOST}),
        client.get("/health", headers={"host": "attacker.example"}),
        client.get("/missing", headers={"host": HOST}),
    ):
        _assert_security_headers(response)


def test_unhandled_exception_returns_generic_secured_500(tmp_path: Path) -> None:
    app = create_app(Settings(data_root=tmp_path / "data"), bound_port=8765)

    @app.get("/_test/error")
    def error_route() -> None:
        raise RuntimeError("sensitive-customer-value")

    response = TestClient(app, raise_server_exceptions=False).get(
        "/_test/error", headers={"host": HOST}
    )

    assert response.status_code == 500
    assert response.text == "内部服务器错误"
    assert "sensitive-customer-value" not in response.text
    _assert_security_headers(response)
