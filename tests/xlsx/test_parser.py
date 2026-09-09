from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from fis.xlsx.errors import WorkbookError
from fis.xlsx.package import SafePackage
from fis.xlsx.parser import ParsedBundle, ParsedItem, ParsedShipment, parse_invoice_bundle
from tests.fixtures.standard_v1_builder import build_standard_v1

OFFICE_RELATIONSHIP = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _rewrite_part(path: Path, part_name: str, rewrite: Callable[[bytes], bytes]) -> None:
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts[part_name] = rewrite(parts[part_name])
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


def _add_shared_strings(
    parts: dict[str, bytes], content: bytes, part_name: str = "xl/sharedStrings.xml"
) -> None:
    parts[part_name] = content
    target = part_name.removeprefix("xl/")
    relationship = (
        f'<Relationship Id="rId9" Type="{OFFICE_RELATIONSHIP}/sharedStrings" Target="{target}"/>'
    ).encode()
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        b"</Relationships>", relationship + b"</Relationships>"
    )


def _template_error(path: Path) -> WorkbookError:
    with pytest.raises(WorkbookError) as captured:
        parse_invoice_bundle(path)
    assert captured.value.code == "TEMPLATE_VERSION_MISMATCH"
    return captured.value


def _workbook_error(path: Path, code: str) -> WorkbookError:
    with pytest.raises(WorkbookError) as captured:
        parse_invoice_bundle(path)
    assert captured.value.code == code
    return captured.value


def test_parses_product_rows_in_first_seen_shipment_order(tmp_path: Path) -> None:
    path = tmp_path / "standard.xlsx"
    build_standard_v1(path, [("S1", 2), ("S2", 1)], [])

    bundle = parse_invoice_bundle(path)

    assert [shipment.shipment_id for shipment in bundle.shipments] == ["S1", "S2"]
    assert [[item.source_row for item in shipment.items] for shipment in bundle.shipments] == [
        [12, 13],
        [14],
    ]
    assert bundle.product_rows == (12, 13, 14)
    assert bundle.separator_row == 15
    assert bundle.total_row == 16
    assert bundle.shipments[0].items[0].cells["A"] == "S1"
    assert bundle.shipments[0].items[0].cells["B"] == "Synthetic Product 1"
    with pytest.raises(TypeError):
        bundle.shipments[0].items[0].cells["A"] = "changed"


def test_rejects_product_row_with_only_one_required_identifier(tmp_path: Path) -> None:
    path = tmp_path / "standard.xlsx"
    build_standard_v1(path, {"S1": 1}, [])

    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(
            b'<c r="B12" t="inlineStr"><is><t>Synthetic Product 1</t></is></c>', b""
        ),
    )

    assert _template_error(path).location == "B12"


def test_resolves_configured_sheet_through_workbook_relationship(tmp_path: Path) -> None:
    path = tmp_path / "renamed-part.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/worksheets/custom.xml"] = parts.pop("xl/worksheets/sheet1.xml")
    parts["xl/worksheets/_rels/custom.xml.rels"] = parts.pop(
        "xl/worksheets/_rels/sheet1.xml.rels"
    )
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        b"worksheets/sheet1.xml", b"worksheets/../worksheets/custom.xml"
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)

    assert parse_invoice_bundle(path).product_rows == (12,)


def test_resolves_literal_dot_segment_in_worksheet_relationship_target(tmp_path: Path) -> None:
    path = tmp_path / "dot-target.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/_rels/workbook.xml.rels",
        lambda xml: xml.replace(b"worksheets/sheet1.xml", b"./worksheets/sheet1.xml"),
    )

    assert parse_invoice_bundle(path).product_rows == (12,)


def test_rejects_workbook_sheets_that_resolve_to_the_same_worksheet_part(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-worksheet-target.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/workbook.xml",
        lambda xml: xml.replace(
            b"</sheets>",
            b'<sheet name="Other" sheetId="2" r:id="rId3"/></sheets>',
        ),
    )
    _rewrite_part(
        path,
        "xl/_rels/workbook.xml.rels",
        lambda xml: xml.replace(
            b"</Relationships>",
            b'<Relationship Id="rId3" '
            b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            b'Target="./worksheets/sheet1.xml"/></Relationships>',
        ),
    )

    assert _template_error(path).location == "xl/workbook.xml"


def test_reads_selected_worksheet_part_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "single-worksheet-read.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    original_read_xml = SafePackage.read_xml
    reads: list[str] = []

    def counted_read_xml(package: SafePackage, part_name: str, max_bytes: int):
        if part_name == "xl/worksheets/sheet1.xml":
            reads.append(part_name)
        return original_read_xml(package, part_name, max_bytes)

    monkeypatch.setattr(SafePackage, "read_xml", counted_read_xml)

    assert parse_invoice_bundle(path).product_rows == (12,)
    assert reads == ["xl/worksheets/sheet1.xml"]


@pytest.mark.parametrize(
    "extra_sheet",
    [
        b'<sheet name="Alpha" sheetId="2" r:id="rId2"/><sheet name="alpha" sheetId="3" r:id="rId3"/>',
        b'<sheet name="Other" r:id="rId2"/>',
        b'<sheet name="Other" sheetId="2" r:id="rId1"/>',
    ],
)
def test_rejects_invalid_metadata_on_unselected_workbook_sheets(
    tmp_path: Path, extra_sheet: bytes
) -> None:
    path = tmp_path / "invalid-sheet-metadata.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/workbook.xml",
        lambda xml: xml.replace(b"</sheets>", extra_sheet + b"</sheets>"),
    )

    assert _template_error(path).location == "xl/workbook.xml"


@pytest.mark.parametrize("relationship_id", ["rId99", "rId2"])
def test_rejects_missing_or_non_worksheet_relation_on_unselected_sheet(
    tmp_path: Path, relationship_id: str
) -> None:
    path = tmp_path / "invalid-unselected-sheet-relation.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/workbook.xml",
        lambda xml: xml.replace(
            b"</sheets>",
            f'<sheet name="Other" sheetId="2" r:id="{relationship_id}"/></sheets>'.encode(),
        ),
    )

    assert _template_error(path).location == "xl/workbook.xml"


def test_parses_configured_worksheet_stored_at_package_root(tmp_path: Path) -> None:
    path = tmp_path / "root-worksheet.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["invoice.xml"] = parts.pop("xl/worksheets/sheet1.xml")
    worksheet_relationships = parts.pop("xl/worksheets/_rels/sheet1.xml.rels")
    parts["_rels/invoice.xml.rels"] = (
        worksheet_relationships.replace(b"../drawings/", b"xl/drawings/")
        .replace(b"../comments1.xml", b"xl/comments1.xml")
    )
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        b"worksheets/sheet1.xml", b"/invoice.xml"
    )
    parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(
        b"/xl/worksheets/sheet1.xml", b"/invoice.xml"
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)

    assert parse_invoice_bundle(path).product_rows == (12,)


def test_rejects_missing_or_duplicate_configured_sheet_and_relationship(tmp_path: Path) -> None:
    path = tmp_path / "bad-workbook.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/workbook.xml",
        lambda xml: xml.replace("装箱单&amp;发票 发货信息".encode(), "其他工作表".encode()),
    )
    assert _template_error(path).location == "xl/workbook.xml"

    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/workbook.xml",
        lambda xml: xml.replace(
            b"</sheets>",
            "<sheet name=\"装箱单&amp;发票 发货信息\" sheetId=\"2\" r:id=\"rId1\"/></sheets>".encode(),
        ),
    )
    assert _template_error(path).location == "xl/workbook.xml"

    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/_rels/workbook.xml.rels",
        lambda xml: xml.replace(
            b"</Relationships>",
            b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        ),
    )
    assert _workbook_error(path, "INVALID_RELATIONSHIP").location == "xl/_rels/workbook.xml.rels:rId1"


def test_reads_shared_rich_inline_and_formula_cells_without_using_cache(tmp_path: Path) -> None:
    path = tmp_path / "cell-types.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(
            b'<c r="D12" t="inlineStr"><is><t>1</t></is></c>',
            b'<c r="D12" t="s"><v>0</v></c>',
        ).replace(
            b'<c r="E12" t="inlineStr"><is><t>Synthetic Unit</t></is></c>',
            b'<c r="E12" t="inlineStr"><is><r><t>Rich </t></r><r><t>text</t></r></is></c>',
        ).replace(
            b'<c r="F12" t="inlineStr"><is><t>1</t></is></c>',
            b'<c r="F12"><f>SUM(2,3)</f><v>5</v></c>',
        ),
    )
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    _add_shared_strings(
        parts,
        b'<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b"<si><r><t>shared </t></r><r><t>rich</t></r></si></sst>",
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)

    cells = parse_invoice_bundle(path).shipments[0].items[0].cells

    assert cells["D"] == "shared rich"
    assert cells["E"] == "Rich text"
    assert cells["F"] == "=SUM(2,3)"


@pytest.mark.parametrize(
    ("target", "part_name"),
    [
        ("custom/strings.xml", "xl/custom/strings.xml"),
        ("/xl/custom/sharedstrings.xml", "xl/custom/SharedStrings.xml"),
    ],
)
def test_reads_shared_strings_from_the_workbook_relationship_target(
    tmp_path: Path, target: str, part_name: str
) -> None:
    path = tmp_path / "related-shared-strings.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(
            b'<c r="D12" t="inlineStr"><is><t>1</t></is></c>',
            b'<c r="D12" t="s"><v>0</v></c>',
        ),
    )
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts[part_name] = (
        b'<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b"<si><t>related string</t></si></sst>"
    )
    relationship = (
        f'<Relationship Id="rId9" Type="{OFFICE_RELATIONSHIP}/sharedStrings" Target="{target}"/>'
    ).encode()
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        b"</Relationships>", relationship + b"</Relationships>"
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)

    assert parse_invoice_bundle(path).shipments[0].items[0].cells["D"] == "related string"


@pytest.mark.parametrize(
    "formula",
    [
        b'<f t="shared" si="0">SUM(D12:E12)</f>',
        b'<f t="shared" si="0"/>',
        b'<f t="array" ref="F12:F13">SUM(D12:E12)</f>',
        b'<f t="dataTable" ref="F12:G13">TABLE(D12,E12)</f>',
    ],
)
def test_rejects_shared_formula_masters_and_followers(tmp_path: Path, formula: bytes) -> None:
    path = tmp_path / "shared-formula.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(
            b'<c r="F12" t="inlineStr"><is><t>1</t></is></c>',
            b'<c r="F12">' + formula + b"<v>1</v></c>",
        ),
    )

    assert _workbook_error(path, "UNSUPPORTED_FORMULA").location == "F12"


@pytest.mark.parametrize(
    "duplicate",
    [
        f'<Relationship Id="rId10" Type="{OFFICE_RELATIONSHIP}/sharedStrings" Target="other.xml"/>',
        f'<Relationship Id="rId10" Type="{OFFICE_RELATIONSHIP}/styles" Target="sharedStrings.xml"/>',
        f'<Relationship Id="rId10" Type="{OFFICE_RELATIONSHIP}/styles" Target="SHAREDSTRINGS.xml"/>',
    ],
)
def test_rejects_duplicate_shared_string_relationship_ids_and_targets(
    tmp_path: Path, duplicate: str
) -> None:
    path = tmp_path / "duplicate-shared-relationship.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    _add_shared_strings(
        parts,
        b'<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
    )
    parts["xl/other.xml"] = b"<other/>"
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        b"</Relationships>", duplicate.encode() + b"</Relationships>"
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)

    assert _template_error(path).location == "xl/_rels/workbook.xml.rels"


@pytest.mark.parametrize("column", ["AB", "BC", "XFD"])
def test_ignores_duplicate_cells_outside_a_to_z(tmp_path: Path, column: str) -> None:
    path = tmp_path / f"later-column-{column}.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    extra = (
        f'<c r="{column}12" t="inlineStr"><is><t>ignored</t></is></c>'
        f'<c r="{column}12" t="inlineStr"><is><t>ignored twice</t></is></c>'
    ).encode()
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(b"</row><row r=\"13\"", extra + b'</row><row r="13"'),
    )

    assert column not in parse_invoice_bundle(path).shipments[0].items[0].cells


def test_rejects_excel_column_beyond_xfd(tmp_path: Path) -> None:
    path = tmp_path / "column-beyond-xfd.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(
            b"</row><row r=\"13\"",
            b'<c r="XYZ12" t="inlineStr"><is><t>invalid</t></is></c></row><row r="13"',
        ),
    )

    assert _template_error(path).location == "XYZ12"


def test_ignores_phonetic_text_in_shared_and_inline_rich_strings(tmp_path: Path) -> None:
    path = tmp_path / "phonetic.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(
            b'<c r="D12" t="inlineStr"><is><t>1</t></is></c>',
            b'<c r="D12" t="s"><v>0</v></c>',
        ).replace(
            b'<c r="E12" t="inlineStr"><is><t>Synthetic Unit</t></is></c>',
            b'<c r="E12" t="inlineStr"><is><t>inline </t><rPh><t>phonetic</t></rPh><r><t>text</t></r></is></c>',
        ),
    )
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    _add_shared_strings(
        parts,
        b'<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<si><t xml:space="preserve">shared </t><rPh><t>phonetic</t></rPh><r><t>text</t></r></si></sst>',
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)

    cells = parse_invoice_bundle(path).shipments[0].items[0].cells

    assert cells["D"] == "shared text"
    assert cells["E"] == "inline text"


@pytest.mark.parametrize("reference", ["D²", "A0", "A01", "XFE12", "A1048577", "A" + "9" * 5000])
def test_rejects_invalid_cell_references_without_leaking_value_errors(
    tmp_path: Path, reference: str
) -> None:
    path = tmp_path / "invalid-reference.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    extra_cell = f'<c r="{reference}" t="inlineStr"><is><t>bad</t></is></c>'.encode()
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(b"</row><row r=\"13\"", extra_cell + b'</row><row r="13"'),
    )

    error = _template_error(path)

    assert error.location == reference


@pytest.mark.parametrize(
    ("row_reference", "case"),
    [("0", "zero"), ("1048577", "past-excel-limit"), ("9" * 5000, "too-long")],
)
def test_rejects_invalid_row_references_as_template_errors(
    tmp_path: Path, row_reference: str, case: str
) -> None:
    path = tmp_path / f"invalid-row-{case}.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(b'<row r="12">', f'<row r="{row_reference}">'.encode()),
    )

    assert _template_error(path).location == "xl/worksheets/sheet1.xml"


def test_parsed_models_copy_mutable_inputs_and_reject_reassignment() -> None:
    original_cells = {"A": "S1"}
    item = ParsedItem("S1", 12, original_cells)
    original_cells["A"] = "changed"
    assert item.cells["A"] == "S1"
    with pytest.raises(TypeError):
        item.cells["A"] = "changed"

    original_items = [item]
    shipment = ParsedShipment("S1", original_items)  # type: ignore[arg-type]
    original_items.clear()
    assert shipment.items == (item,)
    with pytest.raises(AttributeError):
        shipment.items = ()  # type: ignore[misc]

    original_shipments = [shipment]
    original_product_rows = [12]
    bundle = ParsedBundle(original_shipments, original_product_rows, 13, 14)  # type: ignore[arg-type]
    original_shipments.clear()
    original_product_rows.clear()
    assert bundle.shipments == (shipment,)
    assert bundle.product_rows == (12,)
    with pytest.raises(AttributeError):
        bundle.shipments = ()  # type: ignore[misc]


def test_copies_only_a_to_z_when_product_row_has_later_columns(tmp_path: Path) -> None:
    path = tmp_path / "later-columns.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(
            b"</row><row r=\"13\"", b'<c r="AA12" t="inlineStr"><is><t>ignored</t></is></c></row><row r="13"'
        ),
    )

    assert "AA" not in parse_invoice_bundle(path).shipments[0].items[0].cells


@pytest.mark.parametrize(
    ("old", "new", "location"),
    [
        ("货件编号SHIPMENT ID".encode(), "图片PRODUCT PICTURE".encode(), "A11"),
        ("跟踪编号".encode(), "普通标题".encode(), "B11"),
        (b"PRODUCT PICTURE", b"REFERENCE ID", "C11"),
    ],
)
def test_rejects_headers_that_are_moved_or_do_not_match(
    tmp_path: Path, old: bytes, new: bytes, location: str
) -> None:
    path = tmp_path / "bad-header.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(path, "xl/worksheets/sheet1.xml", lambda xml: xml.replace(old, new))

    assert _template_error(path).location == location


def test_requires_empty_separator_and_immediate_normalized_total(tmp_path: Path) -> None:
    path = tmp_path / "separator.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(
            b'<row r="13"/>', b'<row r="13"><c r="C13" t="inlineStr"><is><t>x</t></is></c></row>'
        ),
    )
    assert _template_error(path).location == "C13"

    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace("合计TOTAL".encode(), b"total")
    )
    assert _template_error(path).location == "A14"


def test_rejects_empty_product_region_and_internal_gap(tmp_path: Path) -> None:
    path = tmp_path / "empty.xlsx"
    build_standard_v1(path, {"S1": 1}, [])
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(
            b'<row r="12"><c r="A12" t="inlineStr"><is><t>S1</t></is></c>',
            b'<row r="12"><c r="A12" t="inlineStr"><is><t></t></is></c>',
        ),
    )
    assert _template_error(path).location == "A12"

    build_standard_v1(path, {"S1": 2}, [])
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(
            b'<c r="A13" t="inlineStr"><is><t>S1</t></is></c>',
            b'<c r="A13" t="inlineStr"><is><t></t></is></c>',
        ).replace(
            b'<c r="B13" t="inlineStr"><is><t>Synthetic Product 2</t></is></c>',
            b'<c r="B13" t="inlineStr"><is><t></t></is></c>',
        ),
    )
    assert _template_error(path).location == "C13"
