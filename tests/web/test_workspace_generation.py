from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis.config import Settings
from fis.storage.repositories import DefaultsRepository, MasterDataRepository
from fis.web.app import create_app
from fis.xlsx.errors import WorkbookError
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


def _make_workbook(path: Path, count: int) -> bytes:
    ids = [f"S{index}" for index in range(1, count + 1)]
    rows = list(range(12, 12 + count))
    build_standard_v1(path, ids, rows)
    return path.read_bytes()


def _upload(app: FastAPI, client: TestClient, workbook: bytes) -> str:
    response = client.post(
        "/workspace/invoice",
        headers=mutation_headers(),
        files={"invoice": ("invoice.xlsx", workbook, XLSX_MIME)},
    )
    assert response.status_code == 200
    return app.state.batch_service.store.active_ids()[-1]


def _ready_batch(
    app: FastAPI,
    client: TestClient,
    tmp_path: Path,
    folder_picker: FakeFolderPicker,
    *,
    count: int = 2,
    set_fields: bool = True,
    set_folder: bool = True,
) -> tuple[str, Path]:
    workbook = _make_workbook(tmp_path / "source.xlsx", count)
    batch_id = _upload(app, client, workbook)
    MasterDataRepository(app.state.database).create_fba_warehouse("ONT8", "Ontario, CA 91761")
    if set_fields:
        assert (
            client.post(
                f"/workspace/{batch_id}/fields", headers=mutation_headers(), json=COMMON_FIELDS
            ).status_code
            == 200
        )
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    if set_folder:
        folder_picker.result = output_dir
        assert (
            client.post(
                f"/workspace/{batch_id}/output-folder", headers=mutation_headers()
            ).status_code
            == 200
        )
    return batch_id, output_dir


def test_generation_publishes_complete_batch_to_selected_folder(
    app: FastAPI, client: TestClient, tmp_path: Path, folder_picker: FakeFolderPicker
) -> None:
    batch_id, output_dir = _ready_batch(app, client, tmp_path, folder_picker, count=2)

    response = client.post(f"/workspace/{batch_id}/generate", headers=mutation_headers())

    assert response.status_code == 200
    assert "已成功生成 2 份发票" in response.text
    produced = sorted(path.name for path in output_dir.glob("*.xlsx"))
    assert produced == ["S1_AMAZON-ONT8.xlsx", "S2_AMAZON-ONT8.xlsx"]


def test_generation_without_output_folder_is_blocked(
    app: FastAPI, client: TestClient, tmp_path: Path, folder_picker: FakeFolderPicker
) -> None:
    batch_id, output_dir = _ready_batch(
        app, client, tmp_path, folder_picker, set_folder=False
    )

    response = client.post(f"/workspace/{batch_id}/generate", headers=mutation_headers())

    assert response.status_code == 422
    assert list(output_dir.iterdir()) == []


def test_generation_with_incomplete_fields_is_blocked(
    app: FastAPI, client: TestClient, tmp_path: Path, folder_picker: FakeFolderPicker
) -> None:
    batch_id, output_dir = _ready_batch(
        app, client, tmp_path, folder_picker, set_fields=False
    )

    response = client.post(f"/workspace/{batch_id}/generate", headers=mutation_headers())

    assert response.status_code == 422
    assert list(output_dir.iterdir()) == []


def test_filename_collision_blocks_and_keeps_existing_file(
    app: FastAPI, client: TestClient, tmp_path: Path, folder_picker: FakeFolderPicker
) -> None:
    batch_id, output_dir = _ready_batch(app, client, tmp_path, folder_picker, count=2)
    existing = output_dir / "S1_AMAZON-ONT8.xlsx"
    existing.write_bytes(b"existing-user-file")

    response = client.post(f"/workspace/{batch_id}/generate", headers=mutation_headers())

    assert response.status_code == 422
    assert existing.read_bytes() == b"existing-user-file"
    assert not (output_dir / "S2_AMAZON-ONT8.xlsx").exists()


def test_engine_failure_publishes_no_files(
    app: FastAPI, client: TestClient, tmp_path: Path, folder_picker: FakeFolderPicker
) -> None:
    batch_id, output_dir = _ready_batch(app, client, tmp_path, folder_picker, count=2)

    def boom(source: str, outputs: object, destination: object) -> object:
        raise WorkbookError("BOOM", "刻意失败")

    app.state.batch_service._engine = boom

    response = client.post(f"/workspace/{batch_id}/generate", headers=mutation_headers())

    assert response.status_code == 422
    assert list(output_dir.iterdir()) == []


def test_generate_requires_exact_mutation_headers(
    app: FastAPI, client: TestClient, tmp_path: Path, folder_picker: FakeFolderPicker
) -> None:
    batch_id, _ = _ready_batch(app, client, tmp_path, folder_picker, count=2)

    response = client.post(f"/workspace/{batch_id}/generate", headers={"host": HOST})

    assert response.status_code == 403


def test_generate_unknown_batch_returns_404(client: TestClient) -> None:
    response = client.post(
        "/workspace/11111111-1111-1111-1111-111111111111/generate",
        headers=mutation_headers(),
    )

    assert response.status_code == 404


def test_first_upload_has_no_default_output_folder(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    batch_id = _upload(app, client, _make_workbook(tmp_path / "first.xlsx", 1))

    assert app.state.batch_service.get(batch_id).output_directory is None


def test_choosing_folder_without_generation_does_not_remember_it(
    app: FastAPI, client: TestClient, tmp_path: Path, folder_picker: FakeFolderPicker
) -> None:
    first = _upload(app, client, _make_workbook(tmp_path / "first.xlsx", 1))
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    folder_picker.result = chosen
    assert client.post(
        f"/workspace/{first}/output-folder", headers=mutation_headers()
    ).status_code == 200

    second = _upload(app, client, _make_workbook(tmp_path / "second.xlsx", 1))

    assert app.state.batch_service.get(second).output_directory is None


def test_successful_generation_remembers_output_folder_for_next_upload(
    app: FastAPI, client: TestClient, tmp_path: Path, folder_picker: FakeFolderPicker
) -> None:
    batch_id, output_dir = _ready_batch(app, client, tmp_path, folder_picker, count=1)
    assert client.post(
        f"/workspace/{batch_id}/generate", headers=mutation_headers()
    ).status_code == 200

    second_source = tmp_path / "second" / "source.xlsx"
    second_source.parent.mkdir()
    second = _upload(app, client, _make_workbook(second_source, 1))

    assert app.state.batch_service.get(second).output_directory == output_dir


def test_missing_last_successful_output_folder_is_ignored(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    missing = tmp_path / "missing"
    DefaultsRepository(app.state.database).set(
        "last_successful_output_directory", str(missing)
    )

    batch_id = _upload(app, client, _make_workbook(tmp_path / "source.xlsx", 1))

    assert app.state.batch_service.get(batch_id).output_directory is None
