from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path

import pytest
from lxml import etree

from fis.xlsx import combined
from fis.xlsx.combined import (
    _merge_shared_strings,
    _remap_shared_string_indices,
    assemble_combined_workbook,
    combined_filename,
    combined_sheet_names,
    generate_combined_workbook,
    next_output_path,
    worksheet_titles,
)
from fis.xlsx.errors import WorkbookError
from fis.xlsx.generator import ShipmentOutput, generate_shipment_workbook_bytes
from fis.xlsx.package import MAX_ENTRY, SafePackage
from fis.xlsx.template import STANDARD_V1
from fis.xlsx.validate import ValidationReport
from tests.fixtures.standard_v1_builder import build_standard_v1

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


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

    assert titles == ("AMAZON-ONT_8-A_B", "AMAZON-ONT_8-A_B_2")
    assert all(len(title) <= 31 for title in titles)


def test_worksheet_titles_truncate_before_unique_suffix() -> None:
    long_id = "FBA" + "1" * 40
    requests = (_request(long_id, "ONT8"), _request(long_id, "ONT8"))

    titles = worksheet_titles(requests)

    assert len(titles[0]) == 31
    assert len(titles[1]) == 31
    assert titles[1].endswith("_2")
    assert titles[0].casefold() != titles[1].casefold()


def _relationship_part(part: str) -> str:
    path = Path(part)
    return str(path.parent / "_rels" / f"{path.name}.rels").replace("\\", "/")


def test_assembly_creates_ordered_isolated_sheet_relationship_graphs(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    build_standard_v1(source, {"S2": 1, "S1": 1, "S3": 1}, [12, 13, 14])
    requests = (
        _request("S2", "IAH3"),
        _request("S1", "ONT8"),
        _request("S3", "LAX9"),
    )
    titles = worksheet_titles(requests)
    payloads = tuple(generate_shipment_workbook_bytes(source, request) for request in requests)

    payload = assemble_combined_workbook(payloads, titles)
    output = tmp_path / "combined.xlsx"
    output.write_bytes(payload)

    with SafePackage.open(output) as package:
        workbook = package.read_xml("xl/workbook.xml", MAX_ENTRY)
        sheets = workbook.xpath("./main:sheets/main:sheet", namespaces={"main": MAIN_NS})
        assert [sheet.get("name") for sheet in sheets] == [
            "AMAZON-IAH3-S2",
            "AMAZON-ONT8-S1",
            "AMAZON-LAX9-S3",
        ]

        workbook_rels = package.read_xml("xl/_rels/workbook.xml.rels", MAX_ENTRY)
        relations = {relation.get("Id"): relation for relation in workbook_rels}
        worksheet_parts: list[str] = []
        drawing_parts: list[str] = []
        comment_parts: list[str] = []
        for expected, sheet in zip(("S2", "S1", "S3"), sheets, strict=True):
            relation = relations[sheet.get(f"{{{OFFICE_REL_NS}}}id")]
            worksheet_part = package.resolve_relationship_target(
                "xl/workbook.xml", relation.get("Target") or ""
            )
            worksheet_parts.append(worksheet_part)
            worksheet = package.read_xml(worksheet_part, MAX_ENTRY)
            shipment_text = "".join(
                worksheet.xpath(
                    ".//main:c[@r='A12']//main:t/text()", namespaces={"main": MAIN_NS}
                )
            )
            assert shipment_text == expected

            sheet_rels = package.read_xml(_relationship_part(worksheet_part), MAX_ENTRY)
            for item in sheet_rels.xpath(
                "./pkg:Relationship", namespaces={"pkg": PACKAGE_REL_NS}
            ):
                target = package.resolve_relationship_target(
                    worksheet_part, item.get("Target") or ""
                )
                relation_type = item.get("Type") or ""
                package.read(target, MAX_ENTRY)
                if relation_type.endswith("/drawing"):
                    drawing_parts.append(target)
                if relation_type.endswith("/comments"):
                    comment_parts.append(target)

        assert len(set(worksheet_parts)) == 3
        assert len(set(drawing_parts)) == 3
        assert len(set(comment_parts)) == 3
        for drawing_part in drawing_parts:
            drawing_rels = package.read_xml(_relationship_part(drawing_part), MAX_ENTRY)
            image_targets = [
                package.resolve_relationship_target(drawing_part, relation.get("Target") or "")
                for relation in drawing_rels
            ]
            assert len(image_targets) == 1
            assert all(package.read(target, MAX_ENTRY) for target in image_targets)


def test_assembly_rejects_mismatched_payload_and_title_counts(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    build_standard_v1(source, {"S1": 1}, [12])
    payload = generate_shipment_workbook_bytes(source, _request("S1", "ONT8"))

    with pytest.raises(WorkbookError) as caught:
        assemble_combined_workbook((payload,), ())

    assert caught.value.code == "INVALID_COMBINED_INPUT"


def _shared_string_table(values: tuple[str, ...]) -> bytes:
    items = "".join(f"<si><t>{value}</t></si>" for value in values)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{MAIN_NS}" count="{len(values)}" uniqueCount="{len(values)}">'
        f"{items}</sst>"
    ).encode()


def test_shared_strings_merge_deduplicates_and_maps_each_package() -> None:
    """Each package keeps its own index space until it is remapped."""

    packages = [
        {"xl/sharedStrings.xml": _shared_string_table(("甲", "乙"))},
        {"xl/sharedStrings.xml": _shared_string_table(("乙", "丙"))},
    ]

    table, mappings = _merge_shared_strings(packages)

    assert table is not None
    assert mappings[0] == {0: 0, 1: 1}
    assert mappings[1] == {0: 1, 1: 2}


def test_shared_strings_merge_is_absent_when_no_package_uses_them() -> None:
    table, mappings = _merge_shared_strings([{}, {}])

    assert table is None
    assert mappings == [{}, {}]


def test_remap_rewrites_only_shared_string_cells() -> None:
    worksheet = (
        f'<worksheet xmlns="{MAIN_NS}"><sheetData><row r="1">'
        '<c r="A1" t="s"><v>1</v></c>'
        '<c r="B1"><v>1</v></c>'
        '<c r="C1" t="inlineStr"><is><t>原文</t></is></c>'
        "</row></sheetData></worksheet>"
    ).encode()

    remapped = _remap_shared_string_indices(worksheet, {0: 5, 1: 7})

    assert b'<c r="A1" t="s"><v>7</v></c>' in remapped
    assert b'<c r="B1"><v>1</v></c>' in remapped
    assert "原文".encode() in remapped


def test_generate_combined_workbook_publishes_once_and_preserves_order(tmp_path: Path) -> None:
    source = tmp_path / "invoice.xlsx"
    build_standard_v1(source, {"S2": 1, "S1": 1}, [12, 13])
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    requests = (_request("S2", "IAH3"), _request("S1", "ONT8"))

    path = generate_combined_workbook(source, requests, output_dir, "invoice.xlsx")

    assert path.name == "已处理_invoice.xlsx"
    assert combined_sheet_names(path) == ("AMAZON-IAH3-S2", "AMAZON-ONT8-S1")
    assert [child.name for child in output_dir.iterdir()] == ["已处理_invoice.xlsx"]


def test_generate_combined_workbook_uses_suffix_without_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "invoice.xlsx"
    build_standard_v1(source, {"S1": 1}, [12])
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    existing = output_dir / "已处理_invoice.xlsx"
    existing.write_bytes(b"old")

    result = generate_combined_workbook(
        source, (_request("S1", "ONT8"),), output_dir, "invoice.xlsx"
    )

    assert result.name == "已处理_invoice_2.xlsx"
    assert existing.read_bytes() == b"old"


def test_failed_combined_validation_leaves_no_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "invoice.xlsx"
    build_standard_v1(source, {"S1": 1}, [12])
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    monkeypatch.setattr(
        combined, "validate_combined_workbook", lambda *args, **kwargs: ValidationReport(False, "bad")
    )

    with pytest.raises(WorkbookError, match="bad"):
        generate_combined_workbook(source, (_request("S1", "ONT8"),), output_dir, "invoice.xlsx")

    assert list(output_dir.iterdir()) == []


def test_generate_combined_workbook_rejects_missing_output_directory(tmp_path: Path) -> None:
    source = tmp_path / "invoice.xlsx"
    build_standard_v1(source, {"S1": 1}, [12])

    with pytest.raises(WorkbookError) as caught:
        generate_combined_workbook(source, (_request("S1", "ONT8"),), tmp_path / "missing", "invoice.xlsx")

    assert caught.value.code == "OUTPUT_PATH_INVALID"


def test_generate_combined_workbook_requires_at_least_one_shipment(tmp_path: Path) -> None:
    source = tmp_path / "invoice.xlsx"
    build_standard_v1(source, {"S1": 1}, [12])
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(WorkbookError) as caught:
        generate_combined_workbook(source, (), output_dir, "invoice.xlsx")

    assert caught.value.code == "INVALID_COMBINED_INPUT"


def _with_print_area(source: Path, reference: str) -> None:
    """Add a workbook-level print area to a built fixture, in place."""

    with zipfile.ZipFile(source) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    workbook = etree.fromstring(parts["xl/workbook.xml"])
    sheets = workbook.find(f"{{{MAIN_NS}}}sheets")
    assert sheets is not None
    defined_names = etree.Element(f"{{{MAIN_NS}}}definedNames")
    entry = etree.SubElement(defined_names, f"{{{MAIN_NS}}}definedName")
    entry.set("name", "_xlnm.Print_Area")
    entry.set("localSheetId", "0")
    entry.text = f"'{STANDARD_V1.sheet_name}'!{reference}"
    sheets.addnext(defined_names)
    parts["xl/workbook.xml"] = etree.tostring(workbook, xml_declaration=True, encoding="UTF-8")
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)


def _defined_names(path: Path) -> list[tuple[str | None, str | None, str | None]]:
    with SafePackage.open(path) as package:
        workbook = package.read_xml("xl/workbook.xml", MAX_ENTRY)
    return [
        (name.get("name"), name.get("localSheetId"), name.text)
        for name in workbook.xpath(
            "./main:definedNames/main:definedName", namespaces={"main": MAIN_NS}
        )
    ]


def test_combined_workbook_rescopes_print_area_to_every_sheet(tmp_path: Path) -> None:
    source = tmp_path / "invoice.xlsx"
    build_standard_v1(source, {"S2": 1, "S1": 1}, [12, 13])
    _with_print_area(source, "$A$1:$H$20")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    requests = (_request("S2", "IAH3"), _request("S1", "ONT8"))

    path = generate_combined_workbook(source, requests, output_dir, "invoice.xlsx")

    assert _defined_names(path) == [
        ("_xlnm.Print_Area", "0", "'AMAZON-IAH3-S2'!$A$1:$H$19"),
        ("_xlnm.Print_Area", "1", "'AMAZON-ONT8-S1'!$A$1:$H$19"),
    ]


def test_each_sheet_keeps_its_own_translated_print_area(tmp_path: Path) -> None:
    """Shipments with different item counts print different row ranges."""

    source = tmp_path / "invoice.xlsx"
    build_standard_v1(source, {"S1": 1, "S2": 3}, [12])
    _with_print_area(source, "$A$1:$H$20")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    requests = (_request("S1", "ONT8"), _request("S2", "IAH3"))

    path = generate_combined_workbook(source, requests, output_dir, "invoice.xlsx")

    assert _defined_names(path) == [
        ("_xlnm.Print_Area", "0", "'AMAZON-ONT8-S1'!$A$1:$H$17"),
        ("_xlnm.Print_Area", "1", "'AMAZON-IAH3-S2'!$A$1:$H$19"),
    ]


def test_combined_workbook_without_print_area_declares_no_defined_names(tmp_path: Path) -> None:
    source = tmp_path / "invoice.xlsx"
    build_standard_v1(source, {"S1": 1}, [12])
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    path = generate_combined_workbook(source, (_request("S1", "ONT8"),), output_dir, "invoice.xlsx")

    with SafePackage.open(path) as package:
        workbook = package.read_xml("xl/workbook.xml", MAX_ENTRY)
    assert workbook.xpath("./main:definedNames", namespaces={"main": MAIN_NS}) == []
