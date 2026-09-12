from __future__ import annotations

from datetime import date
from pathlib import Path

from fis.xlsx.combined import combined_filename, next_output_path, worksheet_titles
from fis.xlsx.generator import ShipmentOutput


def _request(shipment_id: str, warehouse_code: str) -> ShipmentOutput:
    return ShipmentOutput(
        shipment_id=shipment_id,
        warehouse_code=warehouse_code,
        warehouse_address="Address",
        channel="Ocean",
        delivery_date=date(2026, 9, 10),
        sailing_date=date(2026, 9, 17),
        delivery_warehouse="Yiwu",
    )


def test_combined_filename_uses_original_stem() -> None:
    assert (
        combined_filename("CC20260903010清关单20260903.xlsx")
        == "已处理_CC20260903010清关单20260903.xlsx"
    )


def test_combined_filename_removes_path_and_control_characters() -> None:
    assert combined_filename("../folder/ invoice\x00.xlsx") == "已处理_invoice.xlsx"


def test_next_output_path_uses_first_free_case_insensitive_suffix(tmp_path: Path) -> None:
    (tmp_path / "已处理_invoice.xlsx").write_bytes(b"old")
    (tmp_path / "已处理_INVOICE_2.xlsx").write_bytes(b"old")

    assert next_output_path(tmp_path, "invoice.xlsx").name == "已处理_invoice_3.xlsx"


def test_worksheet_titles_are_legal_ordered_and_unique() -> None:
    requests = (_request("A/B", "ONT:8"), _request("A\\B", "ONT?8"))

    titles = worksheet_titles(requests)

    assert titles == ("ONT_8-A_B", "ONT_8-A_B_2")
    assert all(len(title) <= 31 for title in titles)


def test_worksheet_titles_truncate_before_unique_suffix() -> None:
    long_id = "FBA" + "1" * 40
    requests = (_request(long_id, "ONT8"), _request(long_id, "ONT8"))

    titles = worksheet_titles(requests)

    assert len(titles[0]) == 31
    assert len(titles[1]) == 31
    assert titles[1].endswith("_2")
    assert titles[0].casefold() != titles[1].casefold()
