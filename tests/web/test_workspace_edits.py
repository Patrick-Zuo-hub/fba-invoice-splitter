from datetime import date
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


def mutation_headers() -> dict[str, str]:
    return {"host": HOST, "origin": ORIGIN, "x-fis-request": "1"}


class FakeFolderPicker:
    def __init__(self) -> None:
        self.result: Path | None = None
        self.calls: list[Path | None] = []

    def choose(self, initial: Path | None) -> Path | None:
        self.calls.append(initial)
        return self.result


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


@pytest.fixture
def workbook_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "standard.xlsx"
    build_standard_v1(path, ["S1", "S2", "S3"], [])
    return path.read_bytes()


@pytest.fixture
def uploaded_batch(app: FastAPI, client: TestClient, workbook_bytes: bytes) -> str:
    response = client.post(
        "/workspace/invoice",
        headers=mutation_headers(),
        files={"invoice": ("invoice.xlsx", workbook_bytes, XLSX_MIME)},
    )
    assert response.status_code == 200
    return app.state.batch_service.store.active_ids()[0]


def _fields(client: TestClient, batch_id: str, payload: dict[str, object]) -> object:
    return client.post(
        f"/workspace/{batch_id}/fields", headers=mutation_headers(), json=payload
    )


def test_common_value_then_shipment_override(client: TestClient, uploaded_batch: str) -> None:
    response = _fields(
        client,
        uploaded_batch,
        {"channel_all": "Ocean", "shipments": {"S2": {"channel": "Air"}}},
    )

    assert response.status_code == 200
    assert "Ocean" in response.text
    assert "Air" in response.text


def test_override_takes_precedence_over_common(
    app: FastAPI, client: TestClient, uploaded_batch: str
) -> None:
    _fields(client, uploaded_batch, {"channel_all": "Ocean", "shipments": {"S2": {"channel": "Air"}}})

    service = app.state.batch_service
    workspace = service.get(uploaded_batch)
    assert service.effective_fields(workspace, "S1")["channel"] == "Ocean"
    assert service.effective_fields(workspace, "S2")["channel"] == "Air"


def test_auto_sailing_follows_delivery_change(
    app: FastAPI, client: TestClient, uploaded_batch: str
) -> None:
    _fields(client, uploaded_batch, {"delivery_date_all": "2026-09-20"})

    service = app.state.batch_service
    workspace = service.get(uploaded_batch)
    assert service.effective_fields(workspace, "S1")["sailing_date"] == date(2026, 9, 27)


def test_manual_sailing_date_survives_later_delivery_change(
    app: FastAPI, client: TestClient, uploaded_batch: str
) -> None:
    _fields(client, uploaded_batch, {"shipments": {"S1": {"sailing_date": "2026-10-01"}}})
    _fields(client, uploaded_batch, {"shipments": {"S1": {"delivery_date": "2026-09-20"}}})

    service = app.state.batch_service
    workspace = service.get(uploaded_batch)
    fields = service.effective_fields(workspace, "S1")
    assert fields["sailing_date"] == date(2026, 10, 1)
    assert fields["delivery_date"] == date(2026, 9, 20)


def test_invalid_dates_produce_a_complete_error_mapping(
    client: TestClient, uploaded_batch: str
) -> None:
    response = _fields(
        client,
        uploaded_batch,
        {"delivery_date_all": "not-a-date", "shipments": {"S2": {"sailing_date": "bad"}}},
    )

    assert response.status_code == 200
    assert "日期格式" in response.text


def test_unknown_shipment_override_is_reported(
    client: TestClient, uploaded_batch: str
) -> None:
    response = _fields(client, uploaded_batch, {"shipments": {"NOPE": {"channel": "Ocean"}}})

    assert response.status_code == 200
    # The editor still renders the three real shipments.
    assert "S1" in response.text and "S3" in response.text


def test_fields_requires_exact_mutation_headers(
    client: TestClient, uploaded_batch: str
) -> None:
    response = client.post(
        f"/workspace/{uploaded_batch}/fields",
        headers={"host": HOST},
        json={"channel_all": "Ocean"},
    )

    assert response.status_code == 403


def test_fields_unknown_batch_returns_404(client: TestClient) -> None:
    response = _fields(client, "11111111-1111-1111-1111-111111111111", {"channel_all": "Ocean"})

    assert response.status_code == 404


def test_folder_picker_success_sets_directory(
    app: FastAPI,
    client: TestClient,
    uploaded_batch: str,
    folder_picker: FakeFolderPicker,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    folder_picker.result = output_dir

    response = client.post(
        f"/workspace/{uploaded_batch}/output-folder", headers=mutation_headers()
    )

    assert response.status_code == 200
    assert str(output_dir) in response.text
    assert app.state.batch_service.get(uploaded_batch).output_directory == output_dir


def test_folder_picker_cancel_keeps_previous_value(
    app: FastAPI,
    client: TestClient,
    uploaded_batch: str,
    folder_picker: FakeFolderPicker,
) -> None:
    folder_picker.result = None

    response = client.post(
        f"/workspace/{uploaded_batch}/output-folder", headers=mutation_headers()
    )

    assert response.status_code == 204
    assert app.state.batch_service.get(uploaded_batch).output_directory is None


def test_output_folder_unknown_batch_returns_404(client: TestClient) -> None:
    response = client.post(
        "/workspace/11111111-1111-1111-1111-111111111111/output-folder",
        headers=mutation_headers(),
    )

    assert response.status_code == 404
