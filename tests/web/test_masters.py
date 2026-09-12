from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis.config import Settings
from fis.storage.repositories import MasterDataRepository
from fis.web.app import create_app

PORT = 8765
HOST = f"127.0.0.1:{PORT}"
ORIGIN = f"http://{HOST}"


def mutation_headers() -> dict[str, str]:
    return {"host": HOST, "origin": ORIGIN, "x-fis-request": "1"}


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return create_app(Settings(data_root=tmp_path / "data"), bound_port=PORT)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=ORIGIN)


def _repo(app: FastAPI) -> MasterDataRepository:
    return MasterDataRepository(app.state.database)


def test_master_page_starts_empty(client: TestClient) -> None:
    response = client.get("/masters", headers={"host": HOST})

    assert response.status_code == 200
    assert "暂无渠道" in response.text


def test_create_channel_appears_in_list(client: TestClient) -> None:
    response = client.post("/masters/channels", headers=mutation_headers(), json={"name": "Ocean"})

    assert response.status_code == 200
    assert "Ocean" in response.text
    assert "启用" in response.text


def test_blank_channel_is_rejected(client: TestClient) -> None:
    response = client.post("/masters/channels", headers=mutation_headers(), json={"name": "   "})

    assert response.status_code == 422


def test_case_insensitive_duplicate_channel_is_rejected(client: TestClient) -> None:
    assert client.post(
        "/masters/channels", headers=mutation_headers(), json={"name": "Ocean"}
    ).status_code == 200

    response = client.post(
        "/masters/channels", headers=mutation_headers(), json={"name": "ocean"}
    )

    assert response.status_code == 422


def test_rename_channel_updates_name(app: FastAPI, client: TestClient) -> None:
    client.post("/masters/channels", headers=mutation_headers(), json={"name": "Ocean"})
    channel_id = _repo(app).list_channels()[0].id

    response = client.post(
        f"/masters/channels/{channel_id}", headers=mutation_headers(), json={"name": "Sea"}
    )

    assert response.status_code == 200
    assert "Sea" in response.text


def test_deactivate_channel_marks_it_inactive(app: FastAPI, client: TestClient) -> None:
    client.post("/masters/channels", headers=mutation_headers(), json={"name": "Ocean"})
    channel_id = _repo(app).list_channels()[0].id

    response = client.post(
        f"/masters/channels/{channel_id}", headers=mutation_headers(), json={"active": False}
    )

    assert response.status_code == 200
    assert "停用" in response.text
    assert _repo(app).list_channels()[0].active is False


def test_rename_unknown_channel_returns_404(client: TestClient) -> None:
    response = client.post(
        "/masters/channels/999", headers=mutation_headers(), json={"name": "Sea"}
    )

    assert response.status_code == 404


def test_create_delivery_warehouse(client: TestClient) -> None:
    response = client.post(
        "/masters/delivery-warehouses",
        headers=mutation_headers(),
        json={"name": "宁波仓", "address": "宁波市北仑区"},
    )

    assert response.status_code == 200
    assert "宁波仓" in response.text
    assert "宁波市北仑区" in response.text


def test_delivery_warehouse_allows_blank_address(app: FastAPI, client: TestClient) -> None:
    response = client.post(
        "/masters/delivery-warehouses",
        headers=mutation_headers(),
        json={"name": "宁波仓", "address": "  "},
    )

    assert response.status_code == 200
    warehouse = _repo(app).list_delivery_warehouses()[0]
    assert warehouse.name == "宁波仓"
    assert warehouse.address == ""
    assert "地址（选填）" in response.text


def test_create_fba_warehouse_normalizes_code(client: TestClient) -> None:
    response = client.post(
        "/masters/fba-warehouses",
        headers=mutation_headers(),
        json={"code": "ont8", "address": "Ontario"},
    )

    assert response.status_code == 200
    assert "ONT8" in response.text


def test_duplicate_fba_warehouse_is_rejected(client: TestClient) -> None:
    client.post(
        "/masters/fba-warehouses",
        headers=mutation_headers(),
        json={"code": "ONT8", "address": "Ontario"},
    )

    response = client.post(
        "/masters/fba-warehouses",
        headers=mutation_headers(),
        json={"code": " ont8 ", "address": "Elsewhere"},
    )

    assert response.status_code == 422


def test_deactivate_fba_warehouse(app: FastAPI, client: TestClient) -> None:
    client.post(
        "/masters/fba-warehouses",
        headers=mutation_headers(),
        json={"code": "ONT8", "address": "Ontario"},
    )

    response = client.post(
        "/masters/fba-warehouses/ONT8", headers=mutation_headers(), json={"active": False}
    )

    assert response.status_code == 200
    assert _repo(app).list_fba_warehouses()[0].active is False


def test_master_mutation_requires_headers(client: TestClient) -> None:
    response = client.post("/masters/channels", headers={"host": HOST}, json={"name": "Ocean"})

    assert response.status_code == 403
