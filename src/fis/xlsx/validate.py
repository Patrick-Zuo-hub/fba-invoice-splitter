"""Independent structural validation for generated FIS V1 workbooks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from lxml import etree  # type: ignore[import-untyped]

from fis.xlsx.drawings import parse_workbook_drawings
from fis.xlsx.errors import WorkbookError
from fis.xlsx.package import MAX_ENTRY, SafePackage
from fis.xlsx.parser import MAIN_NS, ParsedItem, parse_invoice_bundle
from fis.xlsx.template import STANDARD_V1
from fis.xlsx.transform import compute_totals


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
            root = _worksheet_root(package)
            _validate_values(root, expected_values or {})
            _validate_comments(package)
            _validate_totals(root, shipment.items)
            _validate_formula_caches(package, root)
        inventory = parse_workbook_drawings(path, parsed)
        if expected_images is not None and len(inventory.product_images) != expected_images:
            raise WorkbookError("OUTPUT_IMAGE_MISMATCH", "输出工作簿商品图片数量不正确", expected_shipment)
        if any(image.source_row not in parsed.product_rows for image in inventory.product_images):
            raise WorkbookError("OUTPUT_IMAGE_MISMATCH", "输出工作簿图片无法关联到商品行", expected_shipment)
        return ValidationReport(ok=True)
    except WorkbookError as exc:
        return ValidationReport(ok=False, error=exc.message, location=exc.location)
    except (OSError, ValueError, etree.XMLSyntaxError):
        return ValidationReport(ok=False, error="生成工作簿结构校验失败", location=str(path))


def _worksheet_root(package: SafePackage) -> etree._Element:
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
    return package.read_xml(worksheet_part, MAX_ENTRY)


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


def _cell_text(cell: etree._Element) -> str:
    if cell.get("t") == "inlineStr":
        return "".join(cell.xpath("./main:is//main:t/text()", namespaces={"main": MAIN_NS}))
    return cell.findtext(f"{{{MAIN_NS}}}v") or ""


def _validate_values(root: etree._Element, values: Mapping[str, str]) -> None:
    for field, expected in values.items():
        if field not in STANDARD_V1.value_ranges:
            raise WorkbookError("OUTPUT_TEMPLATE_MISMATCH", "输出字段不属于 V1 模板", field)
        reference = STANDARD_V1.value_ranges[field].split(":", 1)[0]
        actual = _cell_text(_cell(root, reference))
        if actual != str(expected):
            raise WorkbookError("OUTPUT_VALUE_MISMATCH", "输出物流字段不正确", reference)


def _validate_comments(package: SafePackage) -> None:
    comments = [name for name in package.names if name.lower().startswith("xl/comments") and name.lower().endswith(".xml")]
    if not comments:
        raise WorkbookError("OUTPUT_COMMENTS_MISSING", "输出工作簿缺少表头批注", "comments")
    root = package.read_xml(comments[0], MAX_ENTRY)
    refs = {item.get("ref") for item in root.xpath("./main:commentList/main:comment", namespaces={"main": MAIN_NS})}
    if not {"L11", "N11", "P11"}.issubset(refs):
        raise WorkbookError("OUTPUT_COMMENTS_MISSING", "输出工作簿表头批注不完整", "comments")


def _validate_formula_caches(package: SafePackage, root: etree._Element) -> None:
    workbook = package.read_xml("xl/workbook.xml", MAX_ENTRY)
    calc_pr = workbook.find(f"{{{MAIN_NS}}}calcPr")
    if calc_pr is None or any(calc_pr.get(name) != "1" for name in ("fullCalcOnLoad", "forceFullCalc")):
        raise WorkbookError("OUTPUT_CALCULATION_MISMATCH", "输出工作簿未启用完整重算", "calcPr")
    total_row = next(
        int(row.get("r") or "0")
        for row in root.xpath("./main:sheetData/main:row", namespaces={"main": MAIN_NS})
        if any(
            item.tag == f"{{{MAIN_NS}}}c"
            and item.get("r") == f"A{row.get('r')}"
            and _cell_text(item) == "合计TOTAL"
            for item in row
        )
    )
    for reference, expected_formula in (("O10", f"P{total_row}/N{total_row}"), ("R10", "G9/G10")):
        cell = _cell(root, reference)
        formula = cell.findtext(f"{{{MAIN_NS}}}f")
        cached = cell.find(f"{{{MAIN_NS}}}v")
        if formula != expected_formula or cached is None or not (cached.text or "").strip():
            raise WorkbookError("OUTPUT_CALCULATION_MISMATCH", "输出公式或缓存值不正确", reference)


def _validate_totals(root: etree._Element, items: tuple[ParsedItem, ...]) -> None:
    # Parsed items expose a stable cells mapping; avoid depending on implementation details.
    rows = [item.cells for item in items]
    totals = compute_totals(rows)
    total_rows: list[int] = []
    for row in root.xpath("./main:sheetData/main:row", namespaces={"main": MAIN_NS}):
        row_number = int(row.get("r") or "0")
        cells = [item for item in row if item.tag == f"{{{MAIN_NS}}}c" and item.get("r") == f"A{row_number}"]
        if len(cells) == 1 and _cell_text(cells[0]) == "合计TOTAL":
            total_rows.append(row_number)
    if len(total_rows) != 1:
        raise WorkbookError("OUTPUT_TEMPLATE_MISMATCH", "输出工作簿缺少唯一合计行", "A")
    total_row = total_rows[0]
    for column, expected in (("K", totals["K"]), ("M", totals["M"]), ("N", totals["N"]), ("P", totals["P"])):
        actual = _cell_text(_cell(root, f"{column}{total_row}"))
        if Decimal(actual) != expected:
            raise WorkbookError("OUTPUT_TOTAL_MISMATCH", "输出工作簿合计不正确", f"{column}{total_row}")
