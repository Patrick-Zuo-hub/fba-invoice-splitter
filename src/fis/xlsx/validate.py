"""Independent structural validation for generated FIS V1 workbooks."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from pathlib import Path, PurePosixPath

from lxml import etree  # type: ignore[import-untyped]

from fis.xlsx.drawings import Anchor, parse_workbook_drawings
from fis.xlsx.errors import WorkbookError
from fis.xlsx.package import MAX_ENTRY, SafePackage
from fis.xlsx.parser import (
    MAIN_NS,
    ParsedItem,
    _parse_shared_strings,
    _workbook_relationships,
    parse_invoice_bundle,
)
from fis.xlsx.template import STANDARD_V1
from fis.xlsx.transform import (
    _range_rectangle,
    _validate_non_overlapping_rectangles,
    compute_totals,
)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    ok: bool
    error: str | None = None
    location: str | None = None


def validate_generated_workbook(
    path: Path | str,
    *,
    expected_shipment: str,
    expected_rows: int | None = None,
    expected_images: int | None = None,
    expected_values: Mapping[str, str] | None = None,
    expected_unchanged_parts: Mapping[str, str] | None = None,
    expected_fixed_images: int | None = None,
    expected_merge_count: int | None = None,
    expected_print_area: bool | None = None,
    expected_row_attributes: Mapping[int, Mapping[str, str]] | None = None,
    expected_cols_xml: bytes | None = None,
    expected_fixed_media: tuple[str, ...] | None = None,
    expected_fixed_anchors: tuple[tuple[int, int, int, int, int, int, int, int], ...] | None = None,
    expected_cell_styles: Mapping[str, str | None] | None = None,
    expected_merge_refs: tuple[str, ...] | None = None,
    expected_print_areas: tuple[tuple[str, str], ...] | None = None,
) -> ValidationReport:
    """Reopen a generated package and verify independent business invariants."""

    try:
        parsed = parse_invoice_bundle(path)
        shipment = next((item for item in parsed.shipments if item.shipment_id == expected_shipment), None)
        if shipment is None or len(parsed.shipments) != 1:
            raise WorkbookError("OUTPUT_SHIPMENT_MISMATCH", "输出工作簿包含错误的货件", expected_shipment)
        if expected_rows is not None and len(shipment.items) != expected_rows:
            raise WorkbookError("OUTPUT_ROW_MISMATCH", "输出工作簿商品行数量不正确", expected_shipment)

        with SafePackage.open(path) as package:
            root, worksheet_part = _worksheet_root(package)
            strings = _shared_strings(package)
            _validate_values(root, expected_values or {}, strings)
            _validate_comments(package, worksheet_part)
            _validate_totals(root, shipment.items, strings)
            totals = _validate_totals(root, shipment.items, strings)
            _validate_formula_caches(package, root, totals)
            _validate_layout(
                root,
                expected_merge_count,
                expected_print_area,
                package,
                expected_row_attributes,
                expected_cols_xml,
                expected_cell_styles,
                expected_merge_refs,
                expected_print_areas,
            )
            _validate_unchanged_parts(package, expected_unchanged_parts or {})
        inventory = parse_workbook_drawings(path, parsed)
        if expected_images is not None and len(inventory.product_images) + len(inventory.fixed_images) != expected_images:
            raise WorkbookError("OUTPUT_IMAGE_MISMATCH", "输出工作簿商品图片数量不正确", expected_shipment)
        if expected_fixed_images is not None and len(inventory.fixed_images) != expected_fixed_images:
            raise WorkbookError("OUTPUT_FIXED_IMAGE_MISMATCH", "输出工作簿固定图片不完整", expected_shipment)
        if expected_fixed_media is not None and tuple(sorted(image.media_part for image in inventory.fixed_images)) != tuple(sorted(expected_fixed_media)):
            raise WorkbookError("OUTPUT_FIXED_IMAGE_MISMATCH", "输出工作簿固定图片关系不正确", expected_shipment)
        if expected_fixed_anchors is not None:
            actual_anchors = tuple(sorted(_anchor_signature(image.anchor) for image in inventory.fixed_images))
            if actual_anchors != tuple(sorted(expected_fixed_anchors)):
                raise WorkbookError("OUTPUT_FIXED_IMAGE_MISMATCH", "输出工作簿固定图片锚点不正确", expected_shipment)
        if any(image.source_row not in parsed.product_rows for image in inventory.product_images):
            raise WorkbookError("OUTPUT_IMAGE_MISMATCH", "输出工作簿图片无法关联到商品行", expected_shipment)
        return ValidationReport(ok=True)
    except WorkbookError as exc:
        return ValidationReport(ok=False, error=exc.message, location=exc.location)
    except (OSError, ValueError, etree.XMLSyntaxError):
        return ValidationReport(ok=False, error="生成工作簿结构校验失败", location=str(path))


def _worksheet_root(package: SafePackage) -> tuple[etree._Element, str]:
    workbook = package.read_xml("xl/workbook.xml", MAX_ENTRY)
    sheet = workbook.xpath(
        "./main:sheets/main:sheet[@name=$name]", name=STANDARD_V1.sheet_name, namespaces={"main": MAIN_NS}
    )
    if len(sheet) != 1:
        raise WorkbookError("OUTPUT_TEMPLATE_MISMATCH", "输出工作簿缺少标准工作表", STANDARD_V1.sheet_name)
    relation_id = sheet[0].get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    if not relation_id:
        raise WorkbookError("OUTPUT_TEMPLATE_MISMATCH", "输出工作表关系无效", STANDARD_V1.sheet_name)
    rels = package.read_xml("xl/_rels/workbook.xml.rels", MAX_ENTRY)
    matches = [item for item in rels if item.get("Id") == relation_id]
    if len(matches) != 1 or not matches[0].get("Target"):
        raise WorkbookError("OUTPUT_TEMPLATE_MISMATCH", "输出工作表关系缺失", relation_id)
    worksheet_part = package.resolve_relationship_target("xl/workbook.xml", matches[0].get("Target") or "")
    return package.read_xml(worksheet_part, MAX_ENTRY), worksheet_part


def _cell(root: etree._Element, reference: str) -> etree._Element:
    row_number = "".join(character for character in reference if character.isdigit())
    row = root.xpath(
        "./main:sheetData/main:row[@r=$row]", row=row_number, namespaces={"main": MAIN_NS}
    )
    if len(row) != 1:
        raise WorkbookError("OUTPUT_TEMPLATE_MISMATCH", "输出单元格缺失", reference)
    cells = [item for item in row[0] if item.tag == f"{{{MAIN_NS}}}c" and item.get("r") == reference]
    if len(cells) != 1:
        raise WorkbookError("OUTPUT_TEMPLATE_MISMATCH", "输出单元格缺失", reference)
    return cells[0]


def _shared_strings(package: SafePackage) -> tuple[str, ...]:
    return _parse_shared_strings(package, _workbook_relationships(package))


def _cell_text(cell: etree._Element, shared_strings: tuple[str, ...] = ()) -> str:
    if cell.get("t") == "inlineStr":
        return "".join(cell.xpath("./main:is//main:t/text()", namespaces={"main": MAIN_NS}))
    if cell.get("t") == "s":
        value = cell.findtext(f"{{{MAIN_NS}}}v") or ""
        try:
            index = int(value)
        except ValueError as exc:
            raise WorkbookError("OUTPUT_TEMPLATE_MISMATCH", "共享字符串索引无效", cell.get("r")) from exc
        if index < 0 or index >= len(shared_strings):
            raise WorkbookError("OUTPUT_TEMPLATE_MISMATCH", "共享字符串索引越界", cell.get("r"))
        return shared_strings[index]
    return cell.findtext(f"{{{MAIN_NS}}}v") or ""


def _validate_values(root: etree._Element, values: Mapping[str, str], shared_strings: tuple[str, ...]) -> None:
    for field, expected in values.items():
        if field not in STANDARD_V1.value_ranges:
            raise WorkbookError("OUTPUT_TEMPLATE_MISMATCH", "输出字段不属于 V1 模板", field)
        reference = STANDARD_V1.value_ranges[field].split(":", 1)[0]
        actual = _cell_text(_cell(root, reference), shared_strings)
        if actual != str(expected):
            raise WorkbookError("OUTPUT_VALUE_MISMATCH", "输出物流字段不正确", reference)


def _validate_comments(package: SafePackage, worksheet_part: str) -> None:
    relation_part = str(PurePosixPath(worksheet_part).parent / "_rels" / f"{PurePosixPath(worksheet_part).name}.rels")
    relations = package.read_xml(relation_part, MAX_ENTRY)
    comment_relations = [
        relation
        for relation in relations
        if (relation.get("Type") or "").lower().endswith("/comments")
    ]
    if len(comment_relations) != 1 or not comment_relations[0].get("Target"):
        raise WorkbookError("OUTPUT_COMMENTS_MISSING", "目标工作表批注关系无效", relation_part)
    comments_part = package.resolve_relationship_target(worksheet_part, comment_relations[0].get("Target") or "")
    root = package.read_xml(comments_part, MAX_ENTRY)
    refs = {item.get("ref") for item in root.xpath("./main:commentList/main:comment", namespaces={"main": MAIN_NS})}
    if not {"L11", "N11", "P11"}.issubset(refs):
        raise WorkbookError("OUTPUT_COMMENTS_MISSING", "目标工作表表头批注不完整", comments_part)
    vml_relations = [
        relation
        for relation in relations
        if (relation.get("Type") or "").lower().endswith("/vmldrawing")
    ]
    if len(vml_relations) != 1 or not vml_relations[0].get("Target"):
        raise WorkbookError("OUTPUT_COMMENTS_MISSING", "目标工作表批注 VML 关系无效", relation_part)
    vml_part = package.resolve_relationship_target(worksheet_part, vml_relations[0].get("Target") or "")
    package.read(vml_part, MAX_ENTRY)


def _validate_formula_caches(
    package: SafePackage, root: etree._Element, totals: Mapping[str, Decimal]
) -> None:
    workbook = package.read_xml("xl/workbook.xml", MAX_ENTRY)
    calc_pr = workbook.find(f"{{{MAIN_NS}}}calcPr")
    if calc_pr is None or any(calc_pr.get(name) != "1" for name in ("fullCalcOnLoad", "forceFullCalc")):
        raise WorkbookError("OUTPUT_CALCULATION_MISMATCH", "输出工作簿未启用完整重算", "calcPr")
    strings = _shared_strings(package)
    total_rows = [
        int(row.get("r") or "0")
        for row in root.xpath("./main:sheetData/main:row", namespaces={"main": MAIN_NS})
        if any(
            item.tag == f"{{{MAIN_NS}}}c"
            and item.get("r") == f"A{row.get('r')}"
            and _cell_text(item, strings) == "合计TOTAL"
            for item in row
        )
    ]
    if len(total_rows) != 1:
        raise WorkbookError("OUTPUT_TEMPLATE_MISMATCH", "输出工作簿缺少唯一合计行", "A")
    total_row = total_rows[0]
    for reference, expected_formula in (("O10", f"P{total_row}/N{total_row}"), ("R10", "G9/G10")):
        cell = _cell(root, reference)
        formula = cell.findtext(f"{{{MAIN_NS}}}f")
        cached = cell.find(f"{{{MAIN_NS}}}v")
        if formula != expected_formula or cached is None or not (cached.text or "").strip():
            raise WorkbookError("OUTPUT_CALCULATION_MISMATCH", "输出公式或缓存值不正确", reference)
        try:
            actual = Decimal((cached.text or "").strip())
        except (InvalidOperation, ValueError) as exc:
            raise WorkbookError("OUTPUT_CALCULATION_MISMATCH", "输出公式缓存不是有效数字", reference) from exc
        if not actual.is_finite():
            raise WorkbookError("OUTPUT_CALCULATION_MISMATCH", "输出公式缓存不是有限数字", reference)
        denominator = totals["N"]
        numerator = totals["P"] if reference == "O10" else totals["M"]
        try:
            with localcontext() as context:
                context.prec = 28
                expected = numerator / denominator
                tolerance = max(Decimal("1e-12"), abs(expected) * Decimal("1e-12"))
        except DecimalException as exc:
            raise WorkbookError("OUTPUT_CALCULATION_MISMATCH", "输出公式缓存无法计算", reference) from exc
        if abs(actual - expected) > tolerance:
            raise WorkbookError("OUTPUT_CALCULATION_MISMATCH", "输出公式缓存数值不正确", reference)


def _validate_totals(root: etree._Element, items: tuple[ParsedItem, ...], shared_strings: tuple[str, ...]) -> dict[str, Decimal]:
    # Parsed items expose a stable cells mapping; avoid depending on implementation details.
    rows = [item.cells for item in items]
    totals = compute_totals(rows)
    total_rows: list[int] = []
    for row in root.xpath("./main:sheetData/main:row", namespaces={"main": MAIN_NS}):
        row_number = int(row.get("r") or "0")
        cells = [item for item in row if item.tag == f"{{{MAIN_NS}}}c" and item.get("r") == f"A{row_number}"]
        if len(cells) == 1 and _cell_text(cells[0], shared_strings) == "合计TOTAL":
            total_rows.append(row_number)
    if len(total_rows) != 1:
        raise WorkbookError("OUTPUT_TEMPLATE_MISMATCH", "输出工作簿缺少唯一合计行", "A")
    total_row = total_rows[0]
    for column, expected in (("K", totals["K"]), ("M", totals["M"]), ("N", totals["N"]), ("P", totals["P"])):
        actual = _cell_text(_cell(root, f"{column}{total_row}"), shared_strings)
        try:
            matches = Decimal(actual) == expected
        except (ValueError, ArithmeticError) as exc:
            raise WorkbookError("OUTPUT_TOTAL_MISMATCH", "输出工作簿合计不是有效数字", f"{column}{total_row}") from exc
        if not matches:
            raise WorkbookError("OUTPUT_TOTAL_MISMATCH", "输出工作簿合计不正确", f"{column}{total_row}")
    return totals


def _validate_layout(
    root: etree._Element,
    expected_merge_count: int | None,
    expected_print_area: bool | None,
    package: SafePackage,
    expected_row_attributes: Mapping[int, Mapping[str, str]] | None,
    expected_cols_xml: bytes | None,
    expected_cell_styles: Mapping[str, str | None] | None,
    expected_merge_refs: tuple[str, ...] | None,
    expected_print_areas: tuple[tuple[str, str], ...] | None,
) -> None:
    merges = root.xpath("./main:mergeCells/main:mergeCell", namespaces={"main": MAIN_NS})
    if expected_merge_count is not None and len(merges) != expected_merge_count:
        raise WorkbookError("OUTPUT_LAYOUT_MISMATCH", "输出工作簿合并区域不完整", "mergeCells")
    _assert_non_overlapping_merges(merges)
    required_ranges = set(STANDARD_V1.value_ranges.values())
    actual_ranges = {merge.get("ref") for merge in merges}
    if not required_ranges.issubset(actual_ranges):
        raise WorkbookError("OUTPUT_LAYOUT_MISMATCH", "输出工作簿配置合并区域不完整", "mergeCells")
    if expected_merge_refs is not None and tuple(sorted(actual_ranges)) != tuple(sorted(expected_merge_refs)):
        raise WorkbookError("OUTPUT_LAYOUT_MISMATCH", "输出工作簿合并区域与模板不一致", "mergeCells")
    if expected_row_attributes is not None:
        for number, expected in expected_row_attributes.items():
            rows = root.xpath(
                "./main:sheetData/main:row[@r=$row]", row=str(number), namespaces={"main": MAIN_NS}
            )
            if len(rows) != 1 or {key: value for key, value in rows[0].attrib.items() if key != "r"} != dict(expected):
                raise WorkbookError("OUTPUT_LAYOUT_MISMATCH", "输出工作簿行高或行样式发生变化", f"row {number}")
    if expected_cols_xml is not None:
        cols = root.find(f"{{{MAIN_NS}}}cols")
        actual = etree.tostring(cols, encoding="UTF-8") if cols is not None else None
        if actual != expected_cols_xml:
            raise WorkbookError("OUTPUT_LAYOUT_MISMATCH", "输出工作簿列宽发生变化", "cols")
    if expected_cell_styles is not None:
        actual_styles: dict[str, str | None] = {}
        for cell in root.xpath("./main:sheetData/main:row/main:c", namespaces={"main": MAIN_NS}):
            reference = cell.get("r")
            if reference:
                actual_styles[reference] = cell.get("s")
        if actual_styles != dict(expected_cell_styles):
            raise WorkbookError("OUTPUT_STYLE_MISMATCH", "输出工作簿单元格样式发生变化", "sheetData")
        _validate_style_indices(package, actual_styles)
    if expected_print_area is not None:
        workbook = package.read_xml("xl/workbook.xml", MAX_ENTRY)
        names = workbook.xpath("./main:definedNames/main:definedName[@name='_xlnm.Print_Area']", namespaces={"main": MAIN_NS})
        if bool(names) != expected_print_area:
            raise WorkbookError("OUTPUT_PRINT_MISMATCH", "输出工作簿打印区域不完整", "definedNames")
    if expected_print_areas is not None:
        workbook = package.read_xml("xl/workbook.xml", MAX_ENTRY)
        actual_areas = tuple(
            (
                item.get("localSheetId") or "",
                item.text or "",
            )
            for item in workbook.xpath(
                "./main:definedNames/main:definedName[@name='_xlnm.Print_Area']",
                namespaces={"main": MAIN_NS},
            )
        )
        if actual_areas != expected_print_areas:
            raise WorkbookError("OUTPUT_PRINT_MISMATCH", "输出工作簿打印区域引用发生变化", "definedNames")


def _validate_style_indices(package: SafePackage, styles: Mapping[str, str | None]) -> None:
    styles_root = package.read_xml("xl/styles.xml", MAX_ENTRY)
    cell_xfs = styles_root.find(f"{{{MAIN_NS}}}cellXfs")
    if cell_xfs is None:
        raise WorkbookError("OUTPUT_STYLE_MISMATCH", "输出工作簿缺少单元格样式定义", "styles.xml")
    count = len(cell_xfs)
    for reference, style in styles.items():
        if style is None:
            continue
        try:
            index = int(style)
        except ValueError as exc:
            raise WorkbookError("OUTPUT_STYLE_MISMATCH", "输出单元格样式索引无效", reference) from exc
        if index < 0 or index >= count:
            raise WorkbookError("OUTPUT_STYLE_MISMATCH", "输出单元格样式索引越界", reference)


def _assert_non_overlapping_merges(merges: list[etree._Element]) -> None:
    rectangles: list[tuple[str, tuple[int, int, int, int]]] = []
    for merge in merges:
        reference = merge.get("ref") or ""
        try:
            rectangle = _range_rectangle(reference)
        except WorkbookError:
            raise WorkbookError("OUTPUT_LAYOUT_MISMATCH", "输出合并区域引用无效", reference)
        rectangles.append((reference, rectangle))
    try:
        _validate_non_overlapping_rectangles(rectangles)
    except WorkbookError as exc:
        raise WorkbookError("OUTPUT_LAYOUT_MISMATCH", exc.message, exc.location) from exc


def _validate_unchanged_parts(package: SafePackage, expected: Mapping[str, str]) -> None:
    for name, digest in expected.items():
        actual = hashlib.sha256(package.read(name, MAX_ENTRY)).hexdigest()
        if actual != digest:
            raise WorkbookError("OUTPUT_UNCHANGED_PART_MISMATCH", "未触及的工作簿部件发生变化", name)


def _anchor_signature(anchor: Anchor) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        anchor.from_row,
        anchor.from_col,
        anchor.from_row_off,
        anchor.from_col_off,
        anchor.to_row,
        anchor.to_col,
        anchor.to_row_off,
        anchor.to_col_off,
    )
