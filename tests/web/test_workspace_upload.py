from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis.config import Settings
from fis.services.batches import BatchService, Workspace, WorkspaceBatch, WorkspaceStore
from fis.storage.batch_files import BatchFiles
from fis.web.app import create_app
from fis.xlsx.parser import ParsedBundle, ParsedItem, ParsedShipment
from tests.fixtures.standard_v1_builder import build_standard_v1

PORT = 8765
HOST = f"127.0.0.1:{PORT}"
ORIGIN = f"http://{HOST}"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def mutation_headers() -> dict[str, str]:
    return {"host": HOST, "origin": ORIGIN, "x-fis-request": "1"}


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def app(data_root: Path) -> FastAPI:
    return create_app(Settings(data_root=data_root), bound_port=PORT)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=ORIGIN)


@pytest.fixture
def standard_workbook_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "standard.xlsx"
    build_standard_v1(path, ["S1", "S2", "S3"], [])
    return path.read_bytes()


def _batches_dir(data_root: Path) -> Path:
    return data_root / "batches"


def _no_temporary_batches(data_root: Path) -> bool:
    batches = _batches_dir(data_root)
    return not batches.exists() or not any(batches.iterdir())


def test_index_renders_single_page_upload(client: TestClient) -> None:
    response = client.get("/", headers={"host": HOST})

    assert response.status_code == 200
    assert "FBA 发票拆分工具" in response.text
    assert 'name="invoice"' in response.text
    assert "csrf" not in response.text.lower()
    assert "launch" not in response.text.lower()


def test_upload_returns_summary_on_same_page(
    client: TestClient, standard_workbook_bytes: bytes
) -> None:
    response = client.post(
        "/workspace/invoice",
        headers=mutation_headers(),
        files={"invoice": ("invoice.xlsx", standard_workbook_bytes, XLSX_MIME)},
    )

    assert response.status_code == 200
    assert "3 个货件" in response.text
    assert "选择输出文件夹" in response.text
    assert "S1" in response.text and "S2" in response.text and "S3" in response.text
    assert 'class="file-bar"' in response.text
    assert 'id="mapping-panel"' in response.text
    assert 'id="shipment-table"' in response.text
    for heading in (
        "货件单号",
        "总箱数",
        "中文产品品名",
        "FBA 仓库代码",
        "仓库完整地址",
        "运输渠道",
        "提货日期",
        "开船日期",
        "交仓仓库",
        "完整性",
    ):
        assert heading in response.text
    assert response.text.count('data-bulk-field="') == 4
    assert 'type="checkbox"' not in response.text
    assert 'id="save-status"' in response.text
    assert 'class="action-bar"' in response.text
    assert 'class="sticky-col sticky-col-1"' in response.text
    assert 'class="sticky-col sticky-col-2"' in response.text
    assert 'class="sticky-col sticky-col-3"' in response.text


def test_upload_registers_active_workspace_batch(
    app: FastAPI, client: TestClient, standard_workbook_bytes: bytes
) -> None:
    response = client.post(
        "/workspace/invoice",
        headers=mutation_headers(),
        files={"invoice": ("invoice.xlsx", standard_workbook_bytes, XLSX_MIME)},
    )

    assert response.status_code == 200
    active = app.state.batch_service.store.active_ids()
    assert len(active) == 1
    batch = app.state.batch_service.get(active[0])
    assert batch is not None
    assert batch.shipment_count == 3
    assert batch.source_path.is_file()


def test_malformed_zip_is_rejected_and_source_removed(
    client: TestClient, data_root: Path
) -> None:
    response = client.post(
        "/workspace/invoice",
        headers=mutation_headers(),
        files={"invoice": ("broken.xlsx", b"not-a-zip", XLSX_MIME)},
    )

    assert response.status_code == 422
    assert _no_temporary_batches(data_root)


def test_wrong_extension_is_rejected(
    client: TestClient, standard_workbook_bytes: bytes, data_root: Path
) -> None:
    response = client.post(
        "/workspace/invoice",
        headers=mutation_headers(),
        files={"invoice": ("invoice.txt", standard_workbook_bytes, "text/plain")},
    )

    assert response.status_code == 422
    assert _no_temporary_batches(data_root)


def test_duplicate_invoice_field_is_rejected(
    client: TestClient, standard_workbook_bytes: bytes
) -> None:
    response = client.post(
        "/workspace/invoice",
        headers=mutation_headers(),
        files=[
            ("invoice", ("a.xlsx", standard_workbook_bytes, XLSX_MIME)),
            ("invoice", ("b.xlsx", standard_workbook_bytes, XLSX_MIME)),
        ],
    )

    assert response.status_code == 400


def test_missing_invoice_field_is_rejected(client: TestClient) -> None:
    response = client.post("/workspace/invoice", headers=mutation_headers())

    assert response.status_code == 400


def test_oversize_upload_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fis.services.batches.MAX_UPLOAD_BYTES", 16)
    response = client.post(
        "/workspace/invoice",
        headers=mutation_headers(),
        files={"invoice": ("invoice.xlsx", b"x" * 128, XLSX_MIME)},
    )

    assert response.status_code == 413


def test_upload_requires_exact_mutation_headers(
    client: TestClient, standard_workbook_bytes: bytes
) -> None:
    response = client.post(
        "/workspace/invoice",
        headers={"host": HOST},
        files={"invoice": ("invoice.xlsx", standard_workbook_bytes, XLSX_MIME)},
    )

    assert response.status_code == 403


def test_retry_after_failure_succeeds(
    client: TestClient, standard_workbook_bytes: bytes, data_root: Path
) -> None:
    failure = client.post(
        "/workspace/invoice",
        headers=mutation_headers(),
        files={"invoice": ("broken.xlsx", b"not-a-zip", XLSX_MIME)},
    )
    assert failure.status_code == 422

    success = client.post(
        "/workspace/invoice",
        headers=mutation_headers(),
        files={"invoice": ("invoice.xlsx", standard_workbook_bytes, XLSX_MIME)},
    )
    assert success.status_code == 200
    assert "3 个货件" in success.text


def test_editor_view_summarizes_cartons_and_unique_chinese_product_names(
    tmp_path: Path,
) -> None:
    shipment = ParsedShipment(
        "S1",
        (
            ParsedItem("S1", 12, {"E": "收纳箱", "K": "1"}),
            ParsedItem("S1", 13, {"E": "置物架", "K": "2"}),
            ParsedItem("S1", 14, {"E": "收纳箱", "K": "0"}),
            ParsedItem("S1", 15, {"E": "鞋盒", "K": "0"}),
        ),
    )
    batch = WorkspaceBatch(
        id="batch-1",
        source_path=tmp_path / "source.xlsx",
        source_filename="invoice.xlsx",
        source_sha256="abc",
        parsed=ParsedBundle((shipment,), (12, 13, 14, 15), 16, 17),
    )
    service = BatchService(BatchFiles(tmp_path / "data"), WorkspaceStore())

    row = service.editor_view(Workspace(batch=batch))["rows"][0]

    assert row["carton_count"] == 3
    assert row["product_names"] == ("收纳箱", "置物架", "鞋盒")
    assert row["product_summary"] == "收纳箱、置物架，等 3 种产品"
