from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis.config import Settings
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


@pytest.fixture
def folder_picker() -> FakeFolderPicker:
    return FakeFolderPicker()


@pytest.fixture
def app(tmp_path: Path, folder_picker: FakeFolderPicker) -> FastAPI:
    return create_app(
        Settings(data_root=tmp_path / "data"), bound_port=PORT, folder_picker=folder_picker
    )


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=ORIGIN)


def test_single_page_shell_has_sections_and_script(client: TestClient) -> None:
    response = client.get("/", headers={"host": HOST})

    assert response.status_code == 200
    assert 'id="workspace-sections"' in response.text
    assert 'id="invoice-form"' in response.text
    assert "history-panel" in response.text
    assert "masters-panel" in response.text
    assert "/static/app.js" in response.text
    assert "csrf" not in response.text.lower()
    assert "launch" not in response.text.lower()


def test_app_js_is_served_as_javascript(client: TestClient) -> None:
    response = client.get("/static/app.js", headers={"host": HOST})

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "X-FIS-Request" in response.text
    assert "/runtime/heartbeat" in response.text


def test_heartbeat_registers_a_live_page(app: FastAPI, client: TestClient) -> None:
    response = client.post(
        "/runtime/heartbeat", headers=mutation_headers(), json={"page_id": "page-1"}
    )

    assert response.status_code == 204
    assert app.state.lifecycle.snapshot().live_pages == 1


def test_heartbeat_requires_page_id(client: TestClient) -> None:
    response = client.post("/runtime/heartbeat", headers=mutation_headers(), json={})

    assert response.status_code == 400


def test_heartbeat_requires_exact_mutation_headers(client: TestClient) -> None:
    response = client.post("/runtime/heartbeat", headers={"host": HOST}, json={"page_id": "p"})

    assert response.status_code == 403


def test_page_closed_removes_the_page(app: FastAPI, client: TestClient) -> None:
    client.post("/runtime/heartbeat", headers=mutation_headers(), json={"page_id": "page-1"})

    response = client.post(
        "/runtime/page-closed", headers=mutation_headers(), json={"page_id": "page-1"}
    )

    assert response.status_code == 204
    assert app.state.lifecycle.snapshot().live_pages == 0


def test_generation_brackets_a_job_and_releases_it(
    app: FastAPI, client: TestClient, tmp_path: Path, folder_picker: FakeFolderPicker
) -> None:
    source = tmp_path / "source.xlsx"
    build_standard_v1(source, ["S1", "S2"], [12, 13])
    upload = client.post(
        "/workspace/invoice",
        headers=mutation_headers(),
        files={"invoice": ("invoice.xlsx", source.read_bytes(), XLSX_MIME)},
    )
    assert upload.status_code == 200
    batch_id = app.state.batch_service.store.active_ids()[0]
    client.post(f"/workspace/{batch_id}/fields", headers=mutation_headers(), json=COMMON_FIELDS)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    folder_picker.result = output_dir
    client.post(f"/workspace/{batch_id}/output-folder", headers=mutation_headers())

    response = client.post(f"/workspace/{batch_id}/generate", headers=mutation_headers())

    assert response.status_code == 200
    assert app.state.lifecycle.snapshot().active_jobs == 0
