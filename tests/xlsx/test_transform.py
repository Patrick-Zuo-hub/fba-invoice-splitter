from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
from zipfile import ZipFile

import pytest
from lxml import etree

from fis.xlsx.errors import WorkbookError
from fis.xlsx.parser import ParsedBundle, ParsedItem, ParsedShipment, parse_invoice_bundle
from fis.xlsx.template import STANDARD_V1
from fis.xlsx.transform import (
    DRAWING_NS,
    MAIN_NS,
    REL_NS,
    XDR_NS,
    build_row_map,
    compute_totals,
    transform_comments_vml_xml,
    transform_comments_xml,
    transform_drawing_anchors_xml,
    transform_sheet_xml,
    transform_workbook_xml,
)
from tests.fixtures.standard_v1_builder import build_standard_v1


def test_row_map_compacts_target_and_moves_footer() -> None:
    mapping = build_row_map(
        product_rows=(12, 13, 14, 15, 16), kept_rows=(13, 16), total_row=18
    )

    assert mapping == {13: 12, 16: 13, 17: 14, 18: 15}


def test_totals_sum_only_kept_rows() -> None:
    totals = compute_totals([{"K": "7", "M": "115.5", "N": "0.45", "P": "630.0"}])

    assert totals == {
        "K": Decimal(7),
        "M": Decimal("115.5"),
        "N": Decimal("0.45"),
        "P": Decimal("630.0"),
    }


@pytest.mark.parametrize(
    "bad_value",
    [None, "", " ", "abc", "NaN", "Infinity", "-Infinity", True, 1.25],
)
def test_totals_reject_missing_or_non_exact_numbers(bad_value: object) -> None:
    row: dict[str, object] = {"K": "1", "M": "2", "N": "3", "P": "4"}
    row["N"] = bad_value

    with pytest.raises(WorkbookError) as caught:
        compute_totals([row])

    assert caught.value.code == "INVALID_TOTAL_INPUT"
    assert caught.value.location == "N"


def test_totals_are_exact_and_independent_of_global_decimal_context() -> None:
    rows = [
        {
            "K": "12345678901234.5",
            "M": "0.00000000000000000000000000000000000000000000000009",
            "N": "99999999999999",
            "P": "1.00000000000001",
        },
        {
            "K": "0.5",
            "M": "0.00000000000000000000000000000000000000000000000002",
            "N": "1",
            "P": "2.00000000000002",
        },
    ]
    with localcontext() as context:
        context.prec = 3
        totals = compute_totals(rows)

    assert totals["K"] == Decimal(12345678901235)
    assert totals["M"] == Decimal("0.00000000000000000000000000000000000000000000000011")
    assert totals["N"] == Decimal(100000000000000)
    assert totals["P"] == Decimal("3.00000000000003")


@pytest.mark.parametrize(
    "bad_value",
    [
        "1e5000",
        "1e1000000000",
        "1e-5000",
        "1234567890123456",
        "1." + "0" * 200,
    ],
)
def test_totals_reject_values_outside_safe_excel_resource_bounds(bad_value: str) -> None:
    row = {"K": bad_value, "M": "1", "N": "1", "P": "1"}

    with pytest.raises(WorkbookError) as caught:
        compute_totals([row])

    assert caught.value.code == "INVALID_TOTAL_INPUT"
    assert caught.value.location == "K"


@pytest.mark.parametrize(
    ("products", "kept", "total"),
    [
        ((), (), 1),
        ((12, 14), (12,), 16),
        ((12, 13), (), 15),
        ((12, 13), (13, 12), 15),
        ((12, 13), (12, 12), 15),
        ((12, 13), (12, 14), 15),
        ((12, 13), (True,), 15),
        ((12, 13), ([12],), 15),
        ((12, 13), (12,), 14),
    ],
)
def test_row_map_rejects_ambiguous_selections(
    products: tuple[int, ...], kept: tuple[int, ...], total: int
) -> None:
    with pytest.raises(WorkbookError) as caught:
        build_row_map(products, kept, total)

    assert caught.value.code == "INVALID_ROW_SELECTION"


def _cell(row: etree._Element, ref: str, value: str, *, style: str | None = None) -> None:
    attributes = {"r": ref, "t": "inlineStr"}
    if style is not None:
        attributes["s"] = style
    cell = etree.SubElement(row, f"{{{MAIN_NS}}}c", **attributes)
    inline = etree.SubElement(cell, f"{{{MAIN_NS}}}is")
    etree.SubElement(inline, f"{{{MAIN_NS}}}t").text = value


def _formula_cell(row: etree._Element, ref: str, formula: str) -> None:
    cell = etree.SubElement(row, f"{{{MAIN_NS}}}c", r=ref, s="7")
    etree.SubElement(cell, f"{{{MAIN_NS}}}f").text = formula
    etree.SubElement(cell, f"{{{MAIN_NS}}}v").text = "#VALUE!"


def _bundle() -> ParsedBundle:
    shipments = []
    for shipment_id, rows in (("S1", (13, 16)), ("S2", (12, 14, 15))):
        items = tuple(
            ParsedItem(
                shipment_id,
                row,
                {
                    "K": str(row - 10),
                    "M": f"{row - 10}.5",
                    "N": f"0.{row - 10}",
                    "P": str((row - 10) * 10),
                },
            )
            for row in rows
        )
        shipments.append(ParsedShipment(shipment_id, items))
    return ParsedBundle(tuple(shipments), (12, 13, 14, 15, 16), 17, 18)


def _worksheet(*, unsafe_formula: bool = False) -> bytes:
    root = etree.Element(f"{{{MAIN_NS}}}worksheet", nsmap={None: MAIN_NS})
    etree.SubElement(root, f"{{{MAIN_NS}}}dimension", ref="A1:Z20")
    sheet_data = etree.SubElement(root, f"{{{MAIN_NS}}}sheetData")
    for number in range(1, 21):
        row = etree.SubElement(
            sheet_data,
            f"{{{MAIN_NS}}}row",
            r=str(number),
            ht="31" if number == 13 else "15",
        )
        if number == 4:
            _cell(row, "C4", "old warehouse", style="7")
        if number in {5, 6}:
            _cell(row, f"C{number}", "old value", style="7")
        if number in {8, 9}:
            _cell(row, f"C{number}", "old date", style="7")
            _cell(row, f"G{number}", "old summary", style="7")
        if number == 10:
            _cell(row, "C10", "old delivery warehouse", style="7")
            _cell(row, "G10", "old summary", style="7")
        if number in range(12, 17):
            _cell(row, f"A{number}", f"source-{number}", style="3")
        if number == 18:
            _cell(row, "A18", "合计TOTAL")
            for column in "KMNP":
                _cell(row, f"{column}18", "old total", style="7")
        if number == 10:
            _formula_cell(row, "O10", "P18/N18")
            _formula_cell(row, "R10", "G9/G10")
        if number == 19:
            _formula_cell(row, "Q19", "A18" if unsafe_formula else 'IF(A1="A18",1,0)')
    merges = etree.SubElement(
        root, f"{{{MAIN_NS}}}mergeCells", count=str(len(STANDARD_V1.value_ranges) + 1)
    )
    for reference in STANDARD_V1.value_ranges.values():
        etree.SubElement(merges, f"{{{MAIN_NS}}}mergeCell", ref=reference)
    etree.SubElement(merges, f"{{{MAIN_NS}}}mergeCell", ref="A19:B19")
    validations = etree.SubElement(root, f"{{{MAIN_NS}}}dataValidations", count="1")
    etree.SubElement(validations, f"{{{MAIN_NS}}}dataValidation", sqref="A12:A16 C4")
    hyperlinks = etree.SubElement(root, f"{{{MAIN_NS}}}hyperlinks")
    etree.SubElement(hyperlinks, f"{{{MAIN_NS}}}hyperlink", ref="A18")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def _parsed_xml(payload: bytes) -> etree._Element:
    return etree.fromstring(payload)


def _cell_at(root: etree._Element, ref: str) -> etree._Element:
    result = root.xpath(f".//main:c[@r='{ref}']", namespaces={"main": MAIN_NS})
    assert len(result) == 1
    return result[0]


def test_sheet_transform_compacts_rows_writes_values_and_preserves_styles() -> None:
    source = _worksheet()
    parsed = _bundle()

    output = transform_sheet_xml(
        source,
        parsed,
        (13, 16),
        {
            "warehouse_code": "ONT8",
            "delivery_date": date(2026, 9, 10),
        },
    )

    assert source == _worksheet()
    root = _parsed_xml(output)
    row_numbers = [
        int(value)
        for value in root.xpath("./main:sheetData/main:row/@r", namespaces={"main": MAIN_NS})
    ]
    assert row_numbers == list(range(1, 18))
    assert _cell_at(root, "A12").xpath("string(main:is/main:t)", namespaces={"main": MAIN_NS}) == "source-13"
    assert _cell_at(root, "A13").xpath("string(main:is/main:t)", namespaces={"main": MAIN_NS}) == "source-16"
    assert root.xpath("string(./main:sheetData/main:row[@r='12']/@ht)", namespaces={"main": MAIN_NS}) == "31"
    assert _cell_at(root, "C4").get("s") == "7"
    assert _cell_at(root, "C4").xpath("string(main:is/main:t)", namespaces={"main": MAIN_NS}) == "ONT8"
    assert _cell_at(root, "C8").xpath("string(main:is/main:t)", namespaces={"main": MAIN_NS}) == "2026-09-10"
    assert _cell_at(root, "K15").xpath("string(main:v)", namespaces={"main": MAIN_NS}) == "9"
    assert _cell_at(root, "M15").xpath("string(main:v)", namespaces={"main": MAIN_NS}) == "10"
    assert _cell_at(root, "N15").xpath("string(main:v)", namespaces={"main": MAIN_NS}) == "0.9"
    assert _cell_at(root, "P15").xpath("string(main:v)", namespaces={"main": MAIN_NS}) == "90"
    assert _cell_at(root, "G8").xpath("string(main:v)", namespaces={"main": MAIN_NS}) == "9"
    assert _cell_at(root, "G9").xpath("string(main:v)", namespaces={"main": MAIN_NS}) == "10"
    assert _cell_at(root, "G10").xpath("string(main:v)", namespaces={"main": MAIN_NS}) == "0.9"
    assert _cell_at(root, "O10").xpath("string(main:f)", namespaces={"main": MAIN_NS}) == "P15/N15"
    assert _cell_at(root, "O10").xpath("string(main:v)", namespaces={"main": MAIN_NS}) == "100"
    assert _cell_at(root, "R10").xpath(
        "string(main:v)", namespaces={"main": MAIN_NS}
    ) == "11.1111111111111"
    assert root.xpath("./main:mergeCells/main:mergeCell/@ref", namespaces={"main": MAIN_NS}) == [
        *STANDARD_V1.value_ranges.values(),
        "A16:B16",
    ]
    assert root.xpath("string(./main:dataValidations/main:dataValidation/@sqref)", namespaces={"main": MAIN_NS}) == "A12:A13 C4"
    assert root.xpath("string(./main:hyperlinks/main:hyperlink/@ref)", namespaces={"main": MAIN_NS}) == "A15"
    assert root.xpath("string(./main:dimension/@ref)", namespaces={"main": MAIN_NS}) == "A1:Z17"


def test_sheet_transform_accepts_the_official_anonymous_v1_fixture(tmp_path: Path) -> None:
    path = tmp_path / "standard.xlsx"
    build_standard_v1(path, {"S1": 2, "S2": 1}, [12, 13, 14])
    parsed = parse_invoice_bundle(path)
    with ZipFile(path) as archive:
        worksheet = archive.read("xl/worksheets/sheet1.xml")

    output = transform_sheet_xml(
        worksheet,
        parsed,
        (12, 13),
        {"warehouse_code": "ONT8"},
    )

    assert _parsed_xml(output).xpath(
        "string(.//main:c[@r='O10']/main:f)", namespaces={"main": MAIN_NS}
    ) == "P15/N15"


@pytest.mark.parametrize("shipment_number", range(1, 6))
def test_each_five_shipment_fixture_output_ignores_stale_q16_view_state(
    tmp_path: Path, shipment_number: int
) -> None:
    path = tmp_path / "five-shipments.xlsx"
    build_standard_v1(
        path,
        {f"S{number}": 1 for number in range(1, 6)},
        range(12, 17),
    )
    parsed = parse_invoice_bundle(path)
    with ZipFile(path) as archive:
        worksheet = archive.read("xl/worksheets/sheet1.xml")

    output = _parsed_xml(
        transform_sheet_xml(
            worksheet,
            parsed,
            (11 + shipment_number,),
            {},
        )
    )

    assert output.xpath("./main:sheetViews//@topLeftCell", namespaces={"main": MAIN_NS}) == []
    assert output.xpath("./main:sheetViews//@activeCell", namespaces={"main": MAIN_NS}) == []
    assert output.xpath("./main:sheetViews//@sqref", namespaces={"main": MAIN_NS}) == []


def test_sheet_transform_rejects_an_unconfigured_affected_formula() -> None:
    with pytest.raises(WorkbookError) as caught:
        transform_sheet_xml(_worksheet(unsafe_formula=True), _bundle(), (13, 16), {})

    assert caught.value.code == "UNSAFE_FORMULA_TRANSFORM"
    assert caught.value.location == "Q19"


def test_sheet_transform_detects_lowercase_affected_formula_references() -> None:
    root = _parsed_xml(_worksheet())
    _cell_at(root, "Q19").find(f"{{{MAIN_NS}}}f").text = "a18"

    with pytest.raises(WorkbookError) as caught:
        transform_sheet_xml(etree.tostring(root), _bundle(), (13, 16), {})

    assert caught.value.code == "UNSAFE_FORMULA_TRANSFORM"


def test_sheet_transform_rejects_zero_volume_before_writing_formula_cache() -> None:
    parsed = _bundle()
    zero_item = ParsedItem("S1", 13, {"K": "1", "M": "2", "N": "0", "P": "4"})
    other = ParsedItem("S1", 16, {"K": "1", "M": "2", "N": "0", "P": "4"})
    zero_bundle = ParsedBundle(
        (ParsedShipment("S1", (zero_item, other)),),
        parsed.product_rows,
        parsed.separator_row,
        parsed.total_row,
    )

    with pytest.raises(WorkbookError) as caught:
        transform_sheet_xml(_worksheet(), zero_bundle, (13, 16), {})

    assert caught.value.code == "INVALID_TOTAL_INPUT"
    assert caught.value.location == "N"


def test_sheet_transform_rejects_formula_cache_overflow() -> None:
    huge = ParsedItem("S1", 13, {"K": "1", "M": "1e307", "N": "1e-307", "P": "1e307"})
    bundle = ParsedBundle((ParsedShipment("S1", (huge,)),), (13,), 14, 15)
    root = _parsed_xml(_worksheet())
    sheet_data = root.find(f"{{{MAIN_NS}}}sheetData")
    assert sheet_data is not None
    for row in list(sheet_data):
        number = int(row.get("r", "0"))
        if 12 <= number <= 18:
            sheet_data.remove(row)
    product = etree.SubElement(sheet_data, f"{{{MAIN_NS}}}row", r="13")
    _cell(product, "A13", "S1", style="7")
    etree.SubElement(sheet_data, f"{{{MAIN_NS}}}row", r="14")
    total = etree.SubElement(sheet_data, f"{{{MAIN_NS}}}row", r="15")
    _cell(total, "A15", "合计TOTAL", style="7")
    for column in "KMNP":
        _cell(total, f"{column}15", "0", style="7")
    _cell_at(root, "O10").find(f"{{{MAIN_NS}}}f").text = "P15/N15"

    with pytest.raises(WorkbookError) as caught:
        transform_sheet_xml(etree.tostring(root), bundle, (13,), {})

    assert caught.value.code == "INVALID_TOTAL_INPUT"
    assert caught.value.location == "N"


@pytest.mark.parametrize("missing_ref", ["K18", "G8", "O10", "C4"])
def test_sheet_transform_rejects_missing_configured_target_cells(missing_ref: str) -> None:
    root = _parsed_xml(_worksheet())
    cell = _cell_at(root, missing_ref)
    cell.getparent().remove(cell)

    with pytest.raises(WorkbookError) as caught:
        transform_sheet_xml(
            etree.tostring(root), _bundle(), (13, 16), {"warehouse_code": "ONT8"}
        )

    assert caught.value.code == "TEMPLATE_VERSION_MISMATCH"
    assert caught.value.location == missing_ref


def test_sheet_transform_rejects_missing_style_or_unexpected_baseline_formula() -> None:
    missing_style = _parsed_xml(_worksheet())
    _cell_at(missing_style, "G8").attrib.pop("s")
    with pytest.raises(WorkbookError) as caught:
        transform_sheet_xml(etree.tostring(missing_style), _bundle(), (13, 16), {})
    assert caught.value.code == "TEMPLATE_VERSION_MISMATCH"
    assert caught.value.location == "G8"

    wrong_formula = _parsed_xml(_worksheet())
    _cell_at(wrong_formula, "O10").find(f"{{{MAIN_NS}}}f").text = "P17/N17"
    with pytest.raises(WorkbookError) as caught:
        transform_sheet_xml(etree.tostring(wrong_formula), _bundle(), (13, 16), {})
    assert caught.value.code == "UNSAFE_FORMULA_TRANSFORM"
    assert caught.value.location == "O10"


def test_sheet_transform_requires_each_configured_merged_value_range() -> None:
    root = _parsed_xml(_worksheet())
    merge = root.xpath(
        "./main:mergeCells/main:mergeCell[@ref='C4:F4']", namespaces={"main": MAIN_NS}
    )[0]
    merge.getparent().remove(merge)

    with pytest.raises(WorkbookError) as caught:
        transform_sheet_xml(etree.tostring(root), _bundle(), (13, 16), {})

    assert caught.value.code == "TEMPLATE_VERSION_MISMATCH"
    assert caught.value.location == "C4:F4"


@pytest.mark.parametrize("conflicting_ref", ["C4:F4", "C4:G4"])
def test_sheet_transform_rejects_duplicate_or_overlapping_configured_merges(
    conflicting_ref: str,
) -> None:
    root = _parsed_xml(_worksheet())
    merges = root.find(f"{{{MAIN_NS}}}mergeCells")
    assert merges is not None
    etree.SubElement(merges, f"{{{MAIN_NS}}}mergeCell", ref=conflicting_ref)
    merges.set("count", str(len(merges)))

    with pytest.raises(WorkbookError) as caught:
        transform_sheet_xml(etree.tostring(root), _bundle(), (13, 16), {})

    assert caught.value.code == "TEMPLATE_VERSION_MISMATCH"


def test_sheet_transform_is_sheet_aware_and_blocks_current_sheet_whole_rows() -> None:
    other_sheet = _parsed_xml(_worksheet())
    _cell_at(other_sheet, "Q19").find(f"{{{MAIN_NS}}}f").text = (
        "SUM(A:A)+'Other Sheet'!A18"
    )
    transform_sheet_xml(etree.tostring(other_sheet), _bundle(), (13, 16), {})

    current_rows = _parsed_xml(_worksheet())
    _cell_at(current_rows, "Q19").find(f"{{{MAIN_NS}}}f").text = "SUM(12:16)"
    with pytest.raises(WorkbookError) as caught:
        transform_sheet_xml(etree.tostring(current_rows), _bundle(), (13, 16), {})
    assert caught.value.code == "UNSAFE_FORMULA_TRANSFORM"


@pytest.mark.parametrize(
    "formula",
    [
        "SUM('装箱单&发票 发货信息':Other!A14)",
        "SUM(Other!NamedRange)",
    ],
)
def test_sheet_transform_blocks_3d_and_incompletely_parsed_sheet_references(
    formula: str,
) -> None:
    root = _parsed_xml(_worksheet())
    _cell_at(root, "Q19").find(f"{{{MAIN_NS}}}f").text = formula

    with pytest.raises(WorkbookError) as caught:
        transform_sheet_xml(etree.tostring(root), _bundle(), (13, 16), {})

    assert caught.value.code == "UNSAFE_FORMULA_TRANSFORM"


@pytest.mark.parametrize("foreign_location", ["element", "attribute"])
def test_sheet_transform_blocks_reference_nodes_and_attributes_in_foreign_namespaces(
    foreign_location: str,
) -> None:
    evil_ns = "urn:evil"
    root = _parsed_xml(_worksheet())
    if foreign_location == "element":
        etree.SubElement(root, f"{{{evil_ns}}}mergeCell", ref="A14")
    else:
        _cell_at(root, "A12").set(f"{{{evil_ns}}}ref", "A14")

    with pytest.raises(WorkbookError) as caught:
        transform_sheet_xml(etree.tostring(root), _bundle(), (13, 16), {})

    assert caught.value.code == "UNSAFE_REFERENCE"


@pytest.mark.parametrize("kept_row", [12, 13, 14, 15, 16])
def test_sheet_transform_discards_stale_ui_view_references_for_each_shipment(
    kept_row: int,
) -> None:
    root = _parsed_xml(_worksheet())
    views = etree.Element(f"{{{MAIN_NS}}}sheetViews")
    view = etree.SubElement(views, f"{{{MAIN_NS}}}sheetView", workbookViewId="0")
    etree.SubElement(view, f"{{{MAIN_NS}}}pane", topLeftCell="Q16")
    etree.SubElement(view, f"{{{MAIN_NS}}}selection", activeCell="Q16", sqref="Q16")
    root.insert(1, views)

    output = _parsed_xml(
        transform_sheet_xml(etree.tostring(root), _bundle(), (kept_row,), {})
    )

    assert output.xpath("./main:sheetViews//@topLeftCell", namespaces={"main": MAIN_NS}) == []
    assert output.xpath("./main:sheetViews//@activeCell", namespaces={"main": MAIN_NS}) == []
    assert output.xpath("./main:sheetViews//@sqref", namespaces={"main": MAIN_NS}) == []


def test_sheet_transform_updates_conditional_format_and_auto_filter_ranges() -> None:
    root = _parsed_xml(_worksheet())
    etree.SubElement(root, f"{{{MAIN_NS}}}conditionalFormatting", sqref="$A$12:$Z$16")
    etree.SubElement(root, f"{{{MAIN_NS}}}autoFilter", ref="A11:Z16")

    output = _parsed_xml(transform_sheet_xml(etree.tostring(root), _bundle(), (13, 16), {}))

    assert output.xpath(
        "string(./main:conditionalFormatting/@sqref)", namespaces={"main": MAIN_NS}
    ) == "$A$12:$Z$13"
    assert output.xpath(
        "string(./main:autoFilter/@ref)", namespaces={"main": MAIN_NS}
    ) == "A11:Z13"


def test_sheet_transform_rejects_reference_to_removed_row() -> None:
    root = _parsed_xml(_worksheet())
    merge = root.xpath("./main:mergeCells/main:mergeCell", namespaces={"main": MAIN_NS})[-1]
    merge.set("ref", "A14:B14")

    with pytest.raises(WorkbookError) as caught:
        transform_sheet_xml(etree.tostring(root), _bundle(), (13, 16), {})

    assert caught.value.code == "UNSAFE_REFERENCE"


def test_sheet_transform_rejects_affected_validation_formula() -> None:
    root = _parsed_xml(_worksheet())
    validation = root.xpath(
        "./main:dataValidations/main:dataValidation", namespaces={"main": MAIN_NS}
    )[0]
    etree.SubElement(validation, f"{{{MAIN_NS}}}formula1").text = "$A$14"

    with pytest.raises(WorkbookError) as caught:
        transform_sheet_xml(etree.tostring(root), _bundle(), (13, 16), {})

    assert caught.value.code == "UNSAFE_FORMULA_TRANSFORM"


def test_comments_move_and_reject_removed_row_references() -> None:
    root = etree.Element(f"{{{MAIN_NS}}}comments", nsmap={None: MAIN_NS})
    comment_list = etree.SubElement(root, f"{{{MAIN_NS}}}commentList")
    etree.SubElement(comment_list, f"{{{MAIN_NS}}}comment", ref="L11")
    etree.SubElement(comment_list, f"{{{MAIN_NS}}}comment", ref="A19")
    moved = _parsed_xml(transform_comments_xml(etree.tostring(root), _bundle(), (13, 16)))
    assert moved.xpath(".//main:comment/@ref", namespaces={"main": MAIN_NS}) == ["L11", "A16"]

    comment_list[-1].set("ref", "A14")
    with pytest.raises(WorkbookError) as caught:
        transform_comments_xml(etree.tostring(root), _bundle(), (13, 16))
    assert caught.value.code == "UNSAFE_REFERENCE"


def _comments_vml(*, row: str = "18", column: str = "0") -> bytes:
    vml_ns = "urn:schemas-microsoft-com:vml"
    excel_ns = "urn:schemas-microsoft-com:office:excel"
    root = etree.Element(f"{{{vml_ns}}}xml", nsmap={"v": vml_ns, "x": excel_ns})
    shape = etree.SubElement(root, f"{{{vml_ns}}}shape", id="_x0000_s1")
    client = etree.SubElement(shape, f"{{{excel_ns}}}ClientData", ObjectType="Note")
    etree.SubElement(client, f"{{{excel_ns}}}Row").text = row
    etree.SubElement(client, f"{{{excel_ns}}}Column").text = column
    return etree.tostring(root)


def test_comments_vml_moves_zero_based_row_and_validates_comment_cell_alignment() -> None:
    output = _parsed_xml(
        transform_comments_vml_xml(
            _comments_vml(),
            _bundle(),
            (13, 16),
            comment_references=("A19",),
        )
    )
    excel_ns = "urn:schemas-microsoft-com:office:excel"
    assert output.xpath(
        "string(.//x:ClientData/x:Row)", namespaces={"x": excel_ns}
    ) == "15"

    with pytest.raises(WorkbookError) as caught:
        transform_comments_vml_xml(
            _comments_vml(column="1"),
            _bundle(),
            (13, 16),
            comment_references=("A19",),
        )
    assert caught.value.code == "UNSAFE_REFERENCE"


def test_workbook_moves_print_area_and_enables_full_recalculation() -> None:
    root = etree.Element(f"{{{MAIN_NS}}}workbook", nsmap={None: MAIN_NS})
    names = etree.SubElement(root, f"{{{MAIN_NS}}}definedNames")
    print_area = etree.SubElement(names, f"{{{MAIN_NS}}}definedName", name="_xlnm.Print_Area")
    print_area.text = "'装箱单&发票 发货信息'!$A$1:$Z$20"
    etree.SubElement(root, f"{{{MAIN_NS}}}calcPr", calcId="191029")

    output = _parsed_xml(transform_workbook_xml(etree.tostring(root), _bundle(), (13, 16)))

    assert output.xpath("string(.//main:definedName)", namespaces={"main": MAIN_NS}) == "'装箱单&发票 发货信息'!$A$1:$Z$17"
    calc = output.find(f"{{{MAIN_NS}}}calcPr")
    assert calc is not None
    assert calc.attrib == {
        "calcId": "191029",
        "calcMode": "auto",
        "fullCalcOnLoad": "1",
        "forceFullCalc": "1",
    }


def test_workbook_uses_sheet_order_local_id_and_moves_multi_area_print_ranges() -> None:
    root = etree.Element(f"{{{MAIN_NS}}}workbook", nsmap={None: MAIN_NS})
    sheets = etree.SubElement(root, f"{{{MAIN_NS}}}sheets")
    etree.SubElement(sheets, f"{{{MAIN_NS}}}sheet", name="Other", sheetId="1")
    etree.SubElement(sheets, f"{{{MAIN_NS}}}sheet", name="装箱单&发票 发货信息", sheetId="2")
    names = etree.SubElement(root, f"{{{MAIN_NS}}}definedNames")
    other = etree.SubElement(
        names, f"{{{MAIN_NS}}}definedName", name="_xlnm.Print_Area", localSheetId="0"
    )
    other.text = "Other!$A$1:$Z$20"
    target = etree.SubElement(
        names, f"{{{MAIN_NS}}}definedName", name="_xlnm.Print_Area", localSheetId="1"
    )
    target.text = (
        "'装箱单&发票 发货信息'!$A$1:$Z$18,"
        "'装箱单&发票 发货信息'!$A$19:$Z$20"
    )

    output = _parsed_xml(transform_workbook_xml(etree.tostring(root), _bundle(), (13, 16)))
    values = output.xpath(".//main:definedName/text()", namespaces={"main": MAIN_NS})

    assert values == [
        "Other!$A$1:$Z$20",
        (
            "'装箱单&发票 发货信息'!$A$1:$Z$15,"
            "'装箱单&发票 发货信息'!$A$16:$Z$17"
        ),
    ]


def test_workbook_preserves_other_sheet_defined_name_with_escaped_apostrophe() -> None:
    root = etree.Element(f"{{{MAIN_NS}}}workbook", nsmap={None: MAIN_NS})
    sheets = etree.SubElement(root, f"{{{MAIN_NS}}}sheets")
    etree.SubElement(sheets, f"{{{MAIN_NS}}}sheet", name="装箱单&发票 发货信息", sheetId="1")
    etree.SubElement(sheets, f"{{{MAIN_NS}}}sheet", name="Pat's Sheet", sheetId="2")
    names = etree.SubElement(root, f"{{{MAIN_NS}}}definedNames")
    name = etree.SubElement(
        names, f"{{{MAIN_NS}}}definedName", name="OtherName", localSheetId="1"
    )
    name.text = "'Pat''s Sheet'!$A$18"

    output = _parsed_xml(transform_workbook_xml(etree.tostring(root), _bundle(), (13, 16)))

    assert output.xpath(
        "string(.//main:definedName)", namespaces={"main": MAIN_NS}
    ) == "'Pat''s Sheet'!$A$18"


def _drawing() -> bytes:
    root = etree.Element(
        f"{{{XDR_NS}}}wsDr",
        nsmap={"xdr": XDR_NS, "a": DRAWING_NS, "r": REL_NS},
    )
    for source_row, relationship_id in ((13, "rKeep1"), (14, "rDrop"), (16, "rKeep2")):
        anchor = etree.SubElement(root, f"{{{XDR_NS}}}twoCellAnchor")
        start = etree.SubElement(anchor, f"{{{XDR_NS}}}from")
        etree.SubElement(start, f"{{{XDR_NS}}}col").text = "2"
        etree.SubElement(start, f"{{{XDR_NS}}}row").text = str(source_row - 1)
        end = etree.SubElement(anchor, f"{{{XDR_NS}}}to")
        etree.SubElement(end, f"{{{XDR_NS}}}col").text = "3"
        etree.SubElement(end, f"{{{XDR_NS}}}row").text = str(source_row)
        picture = etree.SubElement(anchor, f"{{{XDR_NS}}}pic")
        fill = etree.SubElement(picture, f"{{{XDR_NS}}}blipFill")
        blip = etree.SubElement(fill, f"{{{DRAWING_NS}}}blip")
        blip.set(f"{{{REL_NS}}}embed", relationship_id)
    return etree.tostring(root)


def test_drawing_anchors_move_and_only_explicit_removed_images_are_dropped() -> None:
    output = _parsed_xml(
        transform_drawing_anchors_xml(
            _drawing(),
            _bundle(),
            (13, 16),
            removed_relationship_ids=frozenset({"rDrop"}),
        )
    )

    assert output.xpath("./xdr:twoCellAnchor/xdr:from/xdr:row/text()", namespaces={"xdr": XDR_NS}) == ["11", "12"]
    assert output.xpath("./xdr:twoCellAnchor/xdr:to/xdr:row/text()", namespaces={"xdr": XDR_NS}) == ["12", "13"]
    assert output.xpath(".//a:blip/@r:embed", namespaces={"a": DRAWING_NS, "r": REL_NS}) == ["rKeep1", "rKeep2"]

    with pytest.raises(WorkbookError) as caught:
        transform_drawing_anchors_xml(_drawing(), _bundle(), (13, 16))
    assert caught.value.code == "UNSAFE_REFERENCE"
