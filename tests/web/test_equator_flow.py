from pathlib import Path

from fastapi.testclient import TestClient

from fis.config import Settings
from fis.web.app import create_app
from fis.xlsx.equator import parse_supported_invoice
from tests.fixtures.equator_builder import IDS, build_equator
from tests.web.test_workspace_generation import FakeFolderPicker, mutation_headers


def test_real_routes_upload_mapping_bulk_override_preview_generation_history(tmp_path: Path) -> None:
    source = tmp_path / "sample.xlsx"
    build_equator(source, external=True)
    picker = FakeFolderPicker()
    output = tmp_path / "out"
    output.mkdir()
    picker.result = output
    app = create_app(Settings(tmp_path / "data"), bound_port=8765, folder_picker=picker)
    headers = mutation_headers()
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/workspace/invoice", headers=headers, files={"invoice": ("sample.xlsx", source.read_bytes())})
        assert response.status_code == 200, response.text
        assert "赤道 2.0" in response.text and "深圳博致咨询有限公司" in response.text
        batch = app.state.batch_service.store.active_ids()[-1]
        url = "/workspace/" + batch
        response = client.post(url + "/generate", headers=headers)
        assert response.status_code == 422
        response = client.post(url + "/mapping", headers=headers, json={"mapping": f"SMF3\t{IDS[0]}\nTEB9\t{IDS[1]}"})
        assert response.status_code == 200 and "3923 S B ST" in response.text
        for field, value in (("channel", "美东美森卡派(包税)"), ("delivery_warehouse", "赤道深圳仓")):
            response = client.post(url + "/bulk", headers=headers, json={"field": field, "value": value})
            assert response.status_code == 200, response.text
        response = client.patch(url + "/shipments/" + IDS[1], headers=headers, json={"field": "channel", "value": "美西美森大货中转(不包税)"})
        assert response.status_code == 200 and response.json()["row"]["fields"]["channel_code"] == "CD0209"
        response = client.post(url + "/bulk", headers=headers, json={"field": "arrival_time", "value": "2026-11-25T13:30", "shipment_ids": [IDS[1]]})
        assert response.status_code == 200
        state = app.state.batch_service.require(batch).equator
        assert state.rows[IDS[1]]["arrival_time"] == "2026-11-25T13:30"
        assert state.rows[IDS[0]]["arrival_time"] != "2026-11-25T13:30"
        preview = client.get(url + "/equator-preview", headers={"x-fis-request": "1"})
        assert preview.status_code == 200 and "CD0209" in preview.text
        response = client.patch(url + "/equator-settings", headers=headers, json={"customer_code": ""})
        assert response.status_code == 200 and response.json()["complete_count"] == 0
        assert client.post(url + "/output-folder", headers=headers).status_code == 200
        assert client.post(url + "/generate", headers=headers).status_code == 422
        response = client.patch(url + "/equator-settings", headers=headers, json={"customer_code": "TEST"})
        assert response.json()["complete_count"] == 2
        response = client.post(url + "/generate", headers=headers)
        assert response.status_code == 200, response.text
        assert "全部 4 条明细" in response.text
        generated = next(output.glob("*.xlsx"))
        parsed = parse_supported_invoice(generated)
        assert {i.cells["A"] for s in parsed.shipments for i in s.items} == {"TEST"}
        assert {i.cells["G"] for i in parsed.shipments[1].items} == {"CD0209"}
        history = client.get("/history", headers={"x-fis-request": "1"})
        assert history.status_code == 200 and generated.name in history.text
        with app.state.database.connect() as db:
            row = db.execute("SELECT template_type,template_version FROM processing_batches WHERE id=?", (batch,)).fetchone()
            assert tuple(row) == ("equator", "v2")
