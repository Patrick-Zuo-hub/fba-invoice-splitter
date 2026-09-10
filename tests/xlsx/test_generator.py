from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from zipfile import ZipFile

import pytest

from fis.xlsx.errors import WorkbookError
from fis.xlsx.generator import (
    ShipmentOutput,
    generate_shipment_workbook,
    generate_shipment_workbook_bytes,
)
from fis.xlsx.validate import validate_generated_workbook
from tests.fixtures.standard_v1_builder import build_standard_v1


def _request(shipment_id: str = "S1") -> ShipmentOutput:
    return ShipmentOutput(
        shipment_id=shipment_id,
        warehouse_code="ONT8",
        warehouse_address="Ontario",
        channel="Ocean",
        delivery_date=date(2026, 9, 10),
        sailing_date=date(2026, 9, 17),
        delivery_warehouse="Yiwu",
    )


def test_generation_preserves_target_items_and_images(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    build_standard_v1(source, {"S1": 2, "S2": 1}, [12, 13, 14])

    output = generate_shipment_workbook(source, _request(), tmp_path / "out")

    assert output.name == "S1_ONT8.xlsx"
    report = validate_generated_workbook(output, expected_shipment="S1", expected_rows=2, expected_images=2)
    assert report.ok


def test_generation_is_deterministic_and_supports_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    build_standard_v1(source, {"S1": 2}, [12, 13])
    first = generate_shipment_workbook_bytes(source, _request())
    second = generate_shipment_workbook_bytes(source, _request())
    assert hashlib.sha256(first).digest() == hashlib.sha256(second).digest()


def test_generation_rejects_same_path_and_invalid_shipment(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    build_standard_v1(source, {"S1": 1}, [12])
    with pytest.raises(WorkbookError) as caught:
        generate_shipment_workbook(source, _request(), source)
    assert caught.value.code == "OUTPUT_PATH_CONFLICT"
    with pytest.raises(WorkbookError) as caught:
        generate_shipment_workbook(source, _request("missing"), tmp_path / "out")
    assert caught.value.code == "SHIPMENT_NOT_FOUND"


def test_generation_removes_unselected_media_and_relationships(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    build_standard_v1(source, {"S1": 1, "S2": 1}, [12, 13])
    output = generate_shipment_workbook(source, _request(), tmp_path / "out")
    with ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "xl/media/image1.png" in names
        assert "xl/media/image2.png" not in names
        rels = archive.read("xl/drawings/_rels/drawing1.xml.rels")
        assert b"rId2" not in rels


def test_validation_resolves_shared_string_total_label(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    build_standard_v1(source, {"S1": 1}, [12])
    payload = generate_shipment_workbook_bytes(source, _request())
    from lxml import etree

    from fis.xlsx.validate import _cell_text

    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    cell = etree.Element(f"{{{main}}}c", r="A14", t="s")
    etree.SubElement(cell, f"{{{main}}}v").text = "0"
    assert _cell_text(cell, ("合计TOTAL",)) == "合计TOTAL"
    assert payload


def test_generated_validation_rejects_style_and_merge_mutations(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    build_standard_v1(source, {"S1": 2, "S2": 1}, [12, 13, 14])
    output = generate_shipment_workbook(source, _request(), tmp_path / "out")
    with ZipFile(output) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    from lxml import etree

    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    root = etree.fromstring(parts["xl/worksheets/sheet1.xml"])
    total = root.xpath(".//main:c[@r='K15']", namespaces={"main": main})[0]
    total.set("s", "999")
    merges = root.xpath("./main:mergeCells/main:mergeCell", namespaces={"main": main})
    merges[-1].getparent().remove(merges[-1])
    mutated = tmp_path / "mutated.xlsx"
    parts["xl/worksheets/sheet1.xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    with ZipFile(mutated, "w") as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    report = validate_generated_workbook(mutated, expected_shipment="S1", expected_rows=2, expected_images=2)
    assert not report.ok
