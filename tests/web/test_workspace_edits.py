from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis.config import Settings
from fis.domain.rules import sailing_after
from fis.storage.repositories import MasterDataRepository
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


def test_common_value_then_shipment_override(
    app: FastAPI, client: TestClient, uploaded_batch: str
) -> None:
    masters = MasterDataRepository(app.state.database)
    masters.add_channel("Ocean")
    masters.add_channel("Air")
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
    # The first Monday after the new pickup date, by the one shared rule.
    assert service.effective_fields(workspace, "S1")["sailing_date"] == date(2026, 9, 21)
    assert service.effective_fields(workspace, "S1")["sailing_date"] == sailing_after(
        date(2026, 9, 20)
    )


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


def test_cell_save_updates_only_one_shipment(
    app: FastAPI, client: TestClient, uploaded_batch: str
) -> None:
    MasterDataRepository(app.state.database).add_channel("美森正班")
    response = client.patch(
        f"/workspace/{uploaded_batch}/shipments/S2",
        headers=mutation_headers(),
        json={"field": "channel", "value": "美森正班"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["save_state"] == "saved"
    assert payload["row"]["fields"]["channel"] == "美森正班"
    workspace = app.state.batch_service.get(uploaded_batch)
    assert app.state.batch_service.effective_fields(workspace, "S1")["channel"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    (("channel", "不存在的渠道"), ("delivery_warehouse", "不存在的仓库")),
)
def test_cell_save_requires_active_master_choice(
    client: TestClient, uploaded_batch: str, field: str, value: str
) -> None:
    response = client.patch(
        f"/workspace/{uploaded_batch}/shipments/S1",
        headers=mutation_headers(),
        json={"field": field, "value": value},
    )

    assert response.status_code == 422


def test_cell_save_rejects_non_editable_field(
    client: TestClient, uploaded_batch: str
) -> None:
    response = client.patch(
        f"/workspace/{uploaded_batch}/shipments/S1",
        headers=mutation_headers(),
        json={"field": "carton_count", "value": "99"},
    )

    assert response.status_code == 422


def test_bulk_save_updates_every_shipment(
    app: FastAPI, client: TestClient, uploaded_batch: str
) -> None:
    response = client.post(
        f"/workspace/{uploaded_batch}/bulk",
        headers=mutation_headers(),
        json={"field": "delivery_date", "value": "2026-09-17"},
    )

    assert response.status_code == 200
    assert response.json()["updated"] == 3
    workspace = app.state.batch_service.get(uploaded_batch)
    assert all(
        app.state.batch_service.effective_fields(workspace, shipment_id)["delivery_date"]
        == date(2026, 9, 17)
        for shipment_id in workspace.shipment_ids
    )


def test_bulk_save_rejects_warehouse_code(
    client: TestClient, uploaded_batch: str
) -> None:
    response = client.post(
        f"/workspace/{uploaded_batch}/bulk",
        headers=mutation_headers(),
        json={"field": "warehouse_code", "value": "ONT8"},
    )

    assert response.status_code == 422


def test_editor_view_exposes_only_active_master_choices(
    app: FastAPI, client: TestClient, uploaded_batch: str
) -> None:
    masters = MasterDataRepository(app.state.database)
    active_channel = masters.add_channel("美森正班")
    inactive_channel = masters.add_channel("停用渠道")
    masters.set_channel_active(inactive_channel, False)
    active_warehouse = masters.add_delivery_warehouse("义乌仓", "义乌市")
    inactive_warehouse = masters.add_delivery_warehouse("停用仓", "宁波市")
    masters.set_delivery_warehouse_active(inactive_warehouse, False)

    workspace = app.state.batch_service.get(uploaded_batch)
    view = app.state.batch_service.editor_view(workspace)

    assert view["channel_choices"] == ((active_channel, "美森正班"),)
    assert view["delivery_warehouse_choices"] == ((active_warehouse, "义乌仓"),)


def test_unknown_warehouse_address_can_be_saved_inline(
    app: FastAPI, client: TestClient, uploaded_batch: str
) -> None:
    client.patch(
        f"/workspace/{uploaded_batch}/shipments/S1",
        headers=mutation_headers(),
        json={"field": "warehouse_code", "value": "new1"},
    )

    response = client.post(
        f"/workspace/{uploaded_batch}/shipments/S1/warehouse-address",
        headers=mutation_headers(),
        json={"address": "123 Example Ave, Ontario, CA"},
    )

    assert response.status_code == 200
    assert response.json()["row"]["fields"]["warehouse_address"].startswith("123 Example")
    saved = MasterDataRepository(app.state.database).get_fba_warehouse("NEW1")
    assert saved is not None
    assert saved.address == "123 Example Ave, Ontario, CA"


def test_inline_address_cannot_overwrite_existing_master(
    app: FastAPI, client: TestClient, uploaded_batch: str
) -> None:
    MasterDataRepository(app.state.database).create_fba_warehouse("ONT8", "Original")
    client.patch(
        f"/workspace/{uploaded_batch}/shipments/S1",
        headers=mutation_headers(),
        json={"field": "warehouse_code", "value": "ONT8"},
    )

    response = client.post(
        f"/workspace/{uploaded_batch}/shipments/S1/warehouse-address",
        headers=mutation_headers(),
        json={"address": "Replacement"},
    )

    assert response.status_code == 422
    assert MasterDataRepository(app.state.database).get_fba_warehouse("ONT8").address == "Original"


def test_the_page_shows_the_sailing_date_the_rule_says(
    app: FastAPI, uploaded_batch: str
) -> None:
    """The workspace computed sailing itself instead of asking the rule, so
    changing the rule left what the page actually shows on the old behaviour."""

    service = app.state.batch_service
    workspace = service.get(uploaded_batch)

    for shipment in workspace.parsed.shipments:
        fields = service.effective_fields(workspace, shipment.shipment_id)
        delivery = fields["delivery_date"]
        assert isinstance(delivery, date)
        assert fields["sailing_date"] == sailing_after(delivery)
        assert fields["sailing_date"] != delivery + timedelta(days=7)
