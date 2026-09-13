from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis.config import Settings
from fis.storage.repositories import MasterDataRepository
from fis.web.app import create_app
from tests.fixtures.master_data_builder import (
    CHANNEL_SHEET,
    DELIVERY_SHEET,
    FBA_SHEET,
    build_master_data,
)

PORT = 8765
HOST = f"127.0.0.1:{PORT}"
ORIGIN = f"http://{HOST}"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def mutation_headers() -> dict[str, str]:
    return {"host": HOST, "origin": ORIGIN, "x-fis-request": "1"}


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return create_app(Settings(data_root=tmp_path / "data"), bound_port=PORT)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=ORIGIN)


def _workbook(tmp_path: Path) -> bytes:
    return build_master_data(
        tmp_path / "masters.xlsx",
        sheets={
            FBA_SHEET: [
                ["FBA仓代码", "FBA仓详细地址"],
                ["ONT8", "Ontario"],
                ["IAH3", "Houston"],
            ],
            CHANNEL_SHEET: [["渠道"], ["海运"], ["空运"]],
            DELIVERY_SHEET: [["国内交货仓"], ["义乌仓"]],
        },
    ).read_bytes()


def _upload(client: TestClient, payload: bytes, name: str = "masters.xlsx"):
    return client.post(
        "/masters/import",
        headers=mutation_headers(),
        files={"masters": (name, payload, XLSX_MIME)},
    )


def test_import_writes_master_data_and_reports_counts(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    response = _upload(client, _workbook(tmp_path))

    assert response.status_code == 200
    assert "FBA 仓库：新增 2，更新 0" in response.text
    assert "运输渠道：新增 2，更新 0" in response.text
    assert "送货仓库：新增 1，更新 0" in response.text
    repository = MasterDataRepository(app.state.database)
    assert {w.code for w in repository.list_fba_warehouses()} == {"AMAZON-ONT8", "AMAZON-IAH3"}


def test_reimporting_a_changed_address_reports_an_update(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    MasterDataRepository(app.state.database).create_fba_warehouse("ONT8", "Stale")

    response = _upload(client, _workbook(tmp_path))

    assert "FBA 仓库：新增 1，更新 1" in response.text


def test_a_workbook_missing_a_sheet_is_rejected(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    payload = build_master_data(
        tmp_path / "partial.xlsx",
        sheets={FBA_SHEET: [["FBA仓代码", "FBA仓详细地址"]], CHANNEL_SHEET: [["渠道"]]},
    ).read_bytes()

    response = _upload(client, payload)

    assert response.status_code == 422
    assert DELIVERY_SHEET in response.text
    assert MasterDataRepository(app.state.database).list_channels() == []


def test_a_non_xlsx_upload_is_rejected(client: TestClient) -> None:
    response = _upload(client, b"not a workbook", name="masters.csv")

    assert response.status_code == 400


def test_import_requires_the_mutation_headers(client: TestClient, tmp_path: Path) -> None:
    response = client.post(
        "/masters/import",
        headers={"host": HOST},
        files={"masters": ("masters.xlsx", _workbook(tmp_path), XLSX_MIME)},
    )

    assert response.status_code == 403


def test_an_oversize_upload_is_rejected(client: TestClient) -> None:
    from fis.web.routes.masters import MAX_MASTER_UPLOAD_BYTES

    response = _upload(client, b"x" * (MAX_MASTER_UPLOAD_BYTES + 1))

    assert response.status_code == 413
