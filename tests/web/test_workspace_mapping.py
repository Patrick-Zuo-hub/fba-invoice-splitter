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

COMMON = {
    "channel_all": "海运",
    "delivery_warehouse_all": "宁波仓",
    "delivery_date_all": "2026-09-10",
    "sailing_date_all": "2026-09-17",
}


def mut() -> dict[str, str]:
    return {"host": HOST, "origin": ORIGIN, "x-fis-request": "1"}


class FakeFolderPicker:
    def __init__(self) -> None:
        self.result: Path | None = None

    def choose(self, initial: Path | None) -> Path | None:
        return self.result


@pytest.fixture
def picker() -> FakeFolderPicker:
    return FakeFolderPicker()


@pytest.fixture
def app(tmp_path: Path, picker: FakeFolderPicker) -> FastAPI:
    return create_app(Settings(data_root=tmp_path / "data"), bound_port=PORT, folder_picker=picker)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=ORIGIN)


def _upload(app: FastAPI, client: TestClient, tmp_path: Path, count: int = 2) -> str:
    source = tmp_path / "source.xlsx"
    build_standard_v1(source, [f"S{i}" for i in range(1, count + 1)], list(range(12, 12 + count)))
    response = client.post(
        "/workspace/invoice", headers=mut(),
        files={"invoice": ("invoice.xlsx", source.read_bytes(), XLSX_MIME)},
    )
    assert response.status_code == 200
    return app.state.batch_service.store.active_ids()[0]


def _service(app: FastAPI):
    return app.state.batch_service


def test_paste_mapping_assigns_codes(app: FastAPI, client: TestClient, tmp_path: Path) -> None:
    batch_id = _upload(app, client, tmp_path)
    response = client.post(
        f"/workspace/{batch_id}/mapping", headers=mut(),
        json={"mapping": "S1\tONT8\nS2\tIAH3"},
    )
    assert response.status_code == 200
    assert "ONT8" in response.text and "IAH3" in response.text
    workspace = _service(app).get(batch_id)
    assert _service(app).effective_fields(workspace, "S1")["warehouse_code"] == "ONT8"
    assert _service(app).effective_fields(workspace, "S2")["warehouse_code"] == "IAH3"


def test_address_auto_derived_and_generation_succeeds(
    app: FastAPI, client: TestClient, tmp_path: Path, picker: FakeFolderPicker
) -> None:
    masters = MasterDataRepository(app.state.database)
    masters.create_fba_warehouse("ONT8", "2245 Cactus Ave, Ontario, CA 91761")
    masters.create_fba_warehouse("IAH3", "10384 W Airport Blvd, Houston, TX 77048")
    batch_id = _upload(app, client, tmp_path)
    client.post(f"/workspace/{batch_id}/mapping", headers=mut(), json={"mapping": "S1\tONT8\nS2\tIAH3"})
    client.post(f"/workspace/{batch_id}/fields", headers=mut(), json=COMMON)
    workspace = _service(app).get(batch_id)
    assert "Ontario" in str(_service(app).effective_fields(workspace, "S1")["warehouse_address"])

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    picker.result = output_dir
    client.post(f"/workspace/{batch_id}/output-folder", headers=mut())

    response = client.post(f"/workspace/{batch_id}/generate", headers=mut())
    assert response.status_code == 200
    assert sorted(p.name for p in output_dir.glob("*.xlsx")) == ["S1_ONT8.xlsx", "S2_IAH3.xlsx"]


def test_code_without_master_address_blocks_generation(
    app: FastAPI, client: TestClient, tmp_path: Path, picker: FakeFolderPicker
) -> None:
    MasterDataRepository(app.state.database).create_fba_warehouse("ONT8", "Ontario, CA")
    batch_id = _upload(app, client, tmp_path)
    # S2 mapped to a code that is NOT in Master Data.
    client.post(f"/workspace/{batch_id}/mapping", headers=mut(), json={"mapping": "S1\tONT8\nS2\tZZZ9"})
    client.post(f"/workspace/{batch_id}/fields", headers=mut(), json=COMMON)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    picker.result = output_dir
    client.post(f"/workspace/{batch_id}/output-folder", headers=mut())

    response = client.post(f"/workspace/{batch_id}/generate", headers=mut())
    assert response.status_code == 422
    assert "未在主数据维护地址" in response.text
    assert list(output_dir.iterdir()) == []


def test_unmapped_shipment_blocks_generation(
    app: FastAPI, client: TestClient, tmp_path: Path, picker: FakeFolderPicker
) -> None:
    MasterDataRepository(app.state.database).create_fba_warehouse("ONT8", "Ontario, CA")
    batch_id = _upload(app, client, tmp_path)
    client.post(f"/workspace/{batch_id}/mapping", headers=mut(), json={"mapping": "S1\tONT8"})
    client.post(f"/workspace/{batch_id}/fields", headers=mut(), json=COMMON)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    picker.result = output_dir
    client.post(f"/workspace/{batch_id}/output-folder", headers=mut())

    response = client.post(f"/workspace/{batch_id}/generate", headers=mut())
    assert response.status_code == 422
    assert "未指派 FBA 仓库代码" in response.text


def test_unknown_shipment_in_paste_is_reported(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    batch_id = _upload(app, client, tmp_path)
    _workspace, report = _service(app).apply_mapping(batch_id, "S1\tONT8\nS9\tIAH3")
    assert report["applied"] == {"S1": "ONT8"}
    assert report["unknown"] == ["S9"]
    assert "S2" in report["unmapped"]


def test_malformed_mapping_returns_422(app: FastAPI, client: TestClient, tmp_path: Path) -> None:
    batch_id = _upload(app, client, tmp_path)
    response = client.post(
        f"/workspace/{batch_id}/mapping", headers=mut(), json={"mapping": "S1\tONT8\tEXTRA"}
    )
    assert response.status_code == 422


def test_per_shipment_channel_override(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    batch_id = _upload(app, client, tmp_path)
    client.post(f"/workspace/{batch_id}/mapping", headers=mut(), json={"mapping": "S1\tONT8\nS2\tIAH3"})
    client.post(
        f"/workspace/{batch_id}/fields", headers=mut(),
        json={"channel_all": "海运", "shipments": {"S2": {"channel": "空运"}}},
    )
    workspace = _service(app).get(batch_id)
    assert _service(app).effective_fields(workspace, "S1")["channel"] == "海运"
    assert _service(app).effective_fields(workspace, "S2")["channel"] == "空运"


def test_channel_is_remembered_per_warehouse_code(
    app: FastAPI, client: TestClient, tmp_path: Path, picker: FakeFolderPicker
) -> None:
    MasterDataRepository(app.state.database).create_fba_warehouse("ONT8", "Ontario, CA")
    # First batch: generate S1 -> ONT8 with channel 海运.
    b1 = _upload(app, client, tmp_path, count=1)
    client.post(f"/workspace/{b1}/mapping", headers=mut(), json={"mapping": "S1\tONT8"})
    client.post(f"/workspace/{b1}/fields", headers=mut(), json=COMMON)
    out1 = tmp_path / "out1"
    out1.mkdir()
    picker.result = out1
    client.post(f"/workspace/{b1}/output-folder", headers=mut())
    assert client.post(f"/workspace/{b1}/generate", headers=mut()).status_code == 200

    # Second batch: ONT8 should pre-fill channel 海运 without re-entering it.
    b2 = _upload(app, client, (tmp_path / "b2"), count=1)
    client.post(f"/workspace/{b2}/mapping", headers=mut(), json={"mapping": "S1\tONT8"})
    workspace = _service(app).get(b2)
    assert _service(app).effective_fields(workspace, "S1")["channel"] == "海运"
