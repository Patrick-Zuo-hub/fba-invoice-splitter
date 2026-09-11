import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis.config import Settings
from fis.storage.repositories import MasterDataRepository
from fis.web.app import create_app
from tests.fixtures.standard_v1_builder import build_standard_v1

PORT = 8765
HOST = f"127.0.0.1:{PORT}"
ORIGIN = f"http://{HOST}"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

COMMON_FIELDS = {
    "warehouse_code_all": "ONT8",
    "warehouse_address_all": "Ontario",
    "channel_all": "Ocean",
    "delivery_warehouse_all": "Yiwu",
    "delivery_date_all": "2026-09-10",
    "sailing_date_all": "2026-09-17",
}


def mutation_headers() -> dict[str, str]:
    return {"host": HOST, "origin": ORIGIN, "x-fis-request": "1"}


class FakeFolderPicker:
    def __init__(self) -> None:
        self.result: Path | None = None

    def choose(self, initial: Path | None) -> Path | None:
        return self.result


class FakeFolderOpener:
    def __init__(self) -> None:
        self.opened: list[Path] = []

    def open(self, folder: Path) -> None:
        self.opened.append(folder)


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def folder_picker() -> FakeFolderPicker:
    return FakeFolderPicker()


@pytest.fixture
def app(data_root: Path, folder_picker: FakeFolderPicker) -> FastAPI:
    return create_app(Settings(data_root=data_root), bound_port=PORT, folder_picker=folder_picker)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=ORIGIN)


def _generate(
    app: FastAPI, client: TestClient, tmp_path: Path, folder_picker: FakeFolderPicker
) -> tuple[str, Path]:
    source = tmp_path / "source.xlsx"
    build_standard_v1(source, ["S1", "S2"], [12, 13])
    response = client.post(
        "/workspace/invoice",
        headers=mutation_headers(),
        files={"invoice": ("invoice.xlsx", source.read_bytes(), XLSX_MIME)},
    )
    assert response.status_code == 200
    batch_id = app.state.batch_service.store.active_ids()[0]
    MasterDataRepository(app.state.database).create_fba_warehouse("ONT8", "Ontario, CA 91761")
    client.post(f"/workspace/{batch_id}/fields", headers=mutation_headers(), json=COMMON_FIELDS)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    folder_picker.result = output_dir
    client.post(f"/workspace/{batch_id}/output-folder", headers=mutation_headers())
    assert (
        client.post(f"/workspace/{batch_id}/generate", headers=mutation_headers()).status_code
        == 200
    )
    return batch_id, output_dir


def test_history_lists_completed_batch_with_output_paths(
    app: FastAPI, client: TestClient, tmp_path: Path, folder_picker: FakeFolderPicker
) -> None:
    _generate(app, client, tmp_path, folder_picker)

    response = client.get("/history", headers={"host": HOST})

    assert response.status_code == 200
    assert "invoice.xlsx" in response.text
    assert "S1_ONT8.xlsx" in response.text
    assert "S2_ONT8.xlsx" in response.text


def test_history_database_stores_paths_and_no_workbook_blobs(
    app: FastAPI, client: TestClient, tmp_path: Path, folder_picker: FakeFolderPicker
) -> None:
    batch_id, output_dir = _generate(app, client, tmp_path, folder_picker)

    with closing(sqlite3.connect(app.state.database.path)) as connection:
        connection.row_factory = sqlite3.Row
        batch = connection.execute(
            "SELECT source_path, state FROM processing_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        shipments = connection.execute(
            "SELECT output_path FROM shipments WHERE batch_id = ? ORDER BY shipment_id",
            (batch_id,),
        ).fetchall()
        columns = connection.execute("PRAGMA table_info(processing_batches)").fetchall()
        shipment_columns = connection.execute("PRAGMA table_info(shipments)").fetchall()

    assert batch["source_path"].endswith("source.xlsx")
    assert batch["state"] == "completed"
    assert [row["output_path"] for row in shipments] == [
        str(output_dir / "S1_ONT8.xlsx"),
        str(output_dir / "S2_ONT8.xlsx"),
    ]
    all_columns = list(columns) + list(shipment_columns)
    assert not any(column["type"].upper() == "BLOB" for column in all_columns)


def test_open_folder_invokes_opener_for_existing_directory(
    app: FastAPI, client: TestClient, tmp_path: Path, folder_picker: FakeFolderPicker
) -> None:
    opener = FakeFolderOpener()
    app.state.batch_service._opener = opener
    batch_id, output_dir = _generate(app, client, tmp_path, folder_picker)

    response = client.post(f"/history/{batch_id}/open-folder", headers=mutation_headers())

    assert response.status_code == 204
    assert opener.opened == [output_dir]


def test_open_folder_reports_missing_directory(
    app: FastAPI, client: TestClient, tmp_path: Path, folder_picker: FakeFolderPicker
) -> None:
    batch_id, output_dir = _generate(app, client, tmp_path, folder_picker)
    for child in output_dir.iterdir():
        child.unlink()
    output_dir.rmdir()

    response = client.post(f"/history/{batch_id}/open-folder", headers=mutation_headers())

    assert response.status_code == 422
    assert "文件已移动或删除" in response.text


def test_open_folder_unknown_batch_returns_404(client: TestClient) -> None:
    response = client.post(
        "/history/11111111-1111-1111-1111-111111111111/open-folder",
        headers=mutation_headers(),
    )

    assert response.status_code == 404
