"""Pure, deterministic OOXML transformations for one Shipment worksheet."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from lxml import etree  # type: ignore[import-untyped]

from fis.xlsx.errors import WorkbookError
from fis.xlsx.parser import MAX_EXCEL_ROW, ParsedBundle
from fis.xlsx.template import STANDARD_V1

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_CELL = re.compile(r"^(\$?)([A-Z]{1,3})(\$?)([1-9][0-9]{0,6})$", re.ASCII)
_CELL_IN_FORMULA = re.compile(
    r"(?<![A-Z0-9_.])(?:(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_.]*)!)?"
    r"\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}(?![A-Z0-9_])",
    re.ASCII | re.IGNORECASE,
)
_SQREF_TOKEN = re.compile(
    r"\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}(?::\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6})?",
    re.ASCII,
)
_TOTAL_COLUMNS = ("K", "M", "N", "P")


def _error(code: str, message: str, location: str | None = None) -> WorkbookError:
    return WorkbookError(code, message, location)


def _validate_rows(
    product_rows: tuple[int, ...], kept_rows: tuple[int, ...], total_row: int
) -> None:
    if (
        not product_rows
        or any(isinstance(row, bool) or not isinstance(row, int) for row in product_rows)
        or product_rows[0] < 1
        or product_rows[-1] > MAX_EXCEL_ROW - 2
        or product_rows != tuple(range(product_rows[0], product_rows[-1] + 1))
    ):
        raise _error("INVALID_ROW_SELECTION", "商品行范围无效", "product_rows")
    if total_row != product_rows[-1] + 2:
        raise _error("INVALID_ROW_SELECTION", "合计行位置无效", f"row {total_row}")
    if not kept_rows or len(set(kept_rows)) != len(kept_rows):
        raise _error("INVALID_ROW_SELECTION", "至少保留一个有效商品行", "kept_rows")
    if any(isinstance(row, bool) or not isinstance(row, int) for row in kept_rows):
        raise _error("INVALID_ROW_SELECTION", "保留行不属于商品区域", "kept_rows")
    positions = {row: index for index, row in enumerate(product_rows)}
    try:
        kept_positions = tuple(positions[row] for row in kept_rows)
    except (KeyError, TypeError) as exc:
        raise _error("INVALID_ROW_SELECTION", "保留行不属于商品区域", "kept_rows") from exc
    if kept_positions != tuple(sorted(kept_positions)):
        raise _error("INVALID_ROW_SELECTION", "保留行顺序无效", "kept_rows")


def build_row_map(
    product_rows: tuple[int, ...], kept_rows: tuple[int, ...], total_row: int
) -> dict[int, int]:
    """Map retained product rows and the separator/total rows into compact positions."""

    _validate_rows(product_rows, kept_rows, total_row)
    first = product_rows[0]
    mapping = {old: first + index for index, old in enumerate(kept_rows)}
    mapping[total_row - 1] = first + len(kept_rows)
    mapping[total_row] = first + len(kept_rows) + 1
    return mapping


def _decimal(value: object, column: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column)
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, str):
        if not value.strip():
            raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column)
        try:
            number = Decimal(value.strip())
        except InvalidOperation as exc:
            raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column) from exc
    else:
        # Binary floating point is deliberately not accepted as invoice source data.
        raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column)
    if not number.is_finite():
        raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column)
    return number


def compute_totals(rows: Iterable[Mapping[str, object]]) -> dict[str, Decimal]:
    """Sum required invoice columns exactly, rejecting all implicit coercions."""

    result = {column: Decimal(0) for column in _TOTAL_COLUMNS}
    for row in rows:
        for column in _TOTAL_COLUMNS:
            try:
                value = row[column]
            except (KeyError, TypeError) as exc:
                raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column) from exc
            result[column] += _decimal(value, column)
    return result


class _RowTransform:
    def __init__(self, parsed: ParsedBundle, kept_rows: tuple[int, ...]) -> None:
        if parsed.separator_row != parsed.total_row - 1:
            raise _error(
                "INVALID_ROW_SELECTION", "分隔行与合计行位置无效", f"row {parsed.separator_row}"
            )
        self.product_rows = parsed.product_rows
        self.kept_rows = kept_rows
        self.total_row = parsed.total_row
        self.row_map = build_row_map(parsed.product_rows, kept_rows, parsed.total_row)
        self.removed_rows = frozenset(parsed.product_rows).difference(kept_rows)
        self.shift = len(parsed.product_rows) - len(kept_rows)

    def row(self, old: int) -> int | None:
        if old in self.removed_rows:
            return None
        if old in self.row_map:
            return self.row_map[old]
        if old > self.total_row:
            return old - self.shift
        return old


def _xml_root(payload: bytes, location: str) -> etree._Element:
    if not isinstance(payload, bytes) or b"<!DOCTYPE" in payload.upper():
        raise _error("UNSAFE_XML", "工作簿包含不安全 XML", location)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False, huge_tree=False)
    try:
        root = etree.fromstring(payload, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise _error("INVALID_XML", "工作簿 XML 无法解析", location) from exc
    if root.xpath(".//mc:AlternateContent", namespaces={"mc": MC_NS}):
        raise _error("UNSAFE_REFERENCE", "V1 不支持 AlternateContent", location)
    return root


def _serialize(root: etree._Element) -> bytes:
    return cast(bytes, etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True))


def _parse_cell_ref(reference: str, location: str) -> tuple[str, str, str, int]:
    match = _CELL.fullmatch(reference)
    if match is None:
        raise _error("UNSAFE_REFERENCE", "单元格引用无法安全转换", location)
    col_abs, column, row_abs, row_text = match.groups()
    row = int(row_text)
    if row > MAX_EXCEL_ROW:
        raise _error("UNSAFE_REFERENCE", "单元格引用无法安全转换", location)
    return col_abs, column, row_abs, row


def _translate_cell(reference: str, transform: _RowTransform, location: str) -> str:
    col_abs, column, row_abs, old_row = _parse_cell_ref(reference, location)
    new_row = transform.row(old_row)
    if new_row is None:
        raise _error("UNSAFE_REFERENCE", "引用指向已删除的商品行", location)
    return f"{col_abs}{column}{row_abs}{new_row}"


def _translate_range(
    reference: str,
    transform: _RowTransform,
    location: str,
    *,
    allow_product_compaction: bool = False,
) -> str:
    parts = reference.split(":")
    if len(parts) not in {1, 2}:
        raise _error("UNSAFE_REFERENCE", "区域引用无法安全转换", location)
    if len(parts) == 1:
        return _translate_cell(parts[0], transform, location)
    left = _parse_cell_ref(parts[0], location)
    right = _parse_cell_ref(parts[1], location)
    low, high = sorted((left[3], right[3]))
    deleted = transform.removed_rows.intersection(range(low, high + 1))
    if deleted:
        covers_products = low <= transform.product_rows[0] and high >= transform.product_rows[-1]
        if not allow_product_compaction or not covers_products:
            raise _error("UNSAFE_REFERENCE", "区域引用包含已删除的商品行", location)
        new_low = transform.row(low)
        new_high = transform.row(high)
        if new_low is None:
            new_low = transform.row(transform.kept_rows[0])
        if new_high is None:
            new_high = transform.row(transform.kept_rows[-1])
        if new_low is None or new_high is None:
            raise _error("UNSAFE_REFERENCE", "区域引用无法安全转换", location)
        left_text = f"{left[0]}{left[1]}{left[2]}{new_low}"
        right_text = f"{right[0]}{right[1]}{right[2]}{new_high}"
        return f"{left_text}:{right_text}"
    return (
        f"{left[0]}{left[1]}{left[2]}{transform.row(left[3])}:"
        f"{right[0]}{right[1]}{right[2]}{transform.row(right[3])}"
    )


def _translate_sqref(value: str, transform: _RowTransform, location: str) -> str:
    tokens = value.split()
    if not tokens or any(_SQREF_TOKEN.fullmatch(token) is None for token in tokens):
        raise _error("UNSAFE_REFERENCE", "多区域引用无法安全转换", location)
    return " ".join(
        _translate_range(token, transform, location, allow_product_compaction=True)
        for token in tokens
    )


def _formula_without_strings(formula: str) -> str:
    result: list[str] = []
    index = 0
    in_string = False
    while index < len(formula):
        character = formula[index]
        if character == '"':
            if in_string and index + 1 < len(formula) and formula[index + 1] == '"':
                index += 2
                continue
            in_string = not in_string
            result.append(" ")
        elif not in_string:
            result.append(character)
        index += 1
    if in_string:
        raise _error("UNSAFE_FORMULA_TRANSFORM", "公式字符串无法安全解析", "formula")
    return "".join(result)


def _assert_formula_unaffected(formula: str, transform: _RowTransform, location: str) -> None:
    inspected = _formula_without_strings(formula)
    for match in _CELL_IN_FORMULA.finditer(inspected):
        token = match.group(0).rsplit("!", 1)[-1].upper()
        _ca, _column, _ra, old_row = _parse_cell_ref(token, location)
        if transform.row(old_row) != old_row:
            raise _error(
                "UNSAFE_FORMULA_TRANSFORM", "公式引用受商品行转换影响", location
            )


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite decimal")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _find_or_create_cell(row: etree._Element, reference: str) -> etree._Element:
    for cell in row.findall(f"{{{MAIN_NS}}}c"):
        if cell.get("r") == reference:
            return cell
    cell = etree.Element(f"{{{MAIN_NS}}}c", r=reference)
    target_column = _parse_cell_ref(reference, reference)[1]
    target_number = _column_number(target_column)
    inserted = False
    for index, existing in enumerate(row.findall(f"{{{MAIN_NS}}}c")):
        existing_ref = existing.get("r") or ""
        existing_column = _parse_cell_ref(existing_ref, existing_ref)[1]
        if _column_number(existing_column) > target_number:
            row.insert(index, cell)
            inserted = True
            break
    if not inserted:
        row.append(cell)
    return cell


def _column_number(column: str) -> int:
    result = 0
    for char in column:
        result = result * 26 + ord(char) - ord("A") + 1
    return result


def _clear_value_children(cell: etree._Element) -> None:
    for child in list(cell):
        if child.tag in {
            f"{{{MAIN_NS}}}f",
            f"{{{MAIN_NS}}}v",
            f"{{{MAIN_NS}}}is",
        }:
            cell.remove(child)


def _write_number(cell: etree._Element, value: Decimal) -> None:
    _clear_value_children(cell)
    cell.attrib.pop("t", None)
    etree.SubElement(cell, f"{{{MAIN_NS}}}v").text = _decimal_text(value)


def _write_formula(cell: etree._Element, formula: str, cached: Decimal) -> None:
    _clear_value_children(cell)
    cell.attrib.pop("t", None)
    etree.SubElement(cell, f"{{{MAIN_NS}}}f").text = formula
    etree.SubElement(cell, f"{{{MAIN_NS}}}v").text = _decimal_text(cached)


def _write_inline(cell: etree._Element, value: object) -> None:
    _clear_value_children(cell)
    cell.set("t", "inlineStr")
    inline = etree.SubElement(cell, f"{{{MAIN_NS}}}is")
    text = etree.SubElement(inline, f"{{{MAIN_NS}}}t")
    if isinstance(value, (date, datetime)):
        text.text = value.isoformat()
    elif isinstance(value, (str, int, Decimal)) and not isinstance(value, bool):
        text.text = str(value)
    else:
        raise _error("INVALID_OUTPUT_VALUE", "输出字段值无效", cell.get("r"))
    if text.text and text.text != text.text.strip():
        text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _row_elements(sheet_data: etree._Element) -> dict[int, etree._Element]:
    result: dict[int, etree._Element] = {}
    for row in sheet_data.findall(f"{{{MAIN_NS}}}row"):
        value = row.get("r")
        if value is None or not value.isascii() or not value.isdigit():
            raise _error("UNSAFE_REFERENCE", "工作表行号无法安全转换", value)
        number = int(value)
        if number < 1 or number > MAX_EXCEL_ROW or number in result:
            raise _error("UNSAFE_REFERENCE", "工作表行号无法安全转换", value)
        result[number] = row
    return result


def _move_row(row: etree._Element, old: int, new: int, transform: _RowTransform) -> None:
    row.set("r", str(new))
    for cell in row.findall(f"{{{MAIN_NS}}}c"):
        reference = cell.get("r")
        if reference is None:
            raise _error("UNSAFE_REFERENCE", "单元格缺少引用", f"row {old}")
        _ca, column, _ra, cell_row = _parse_cell_ref(reference, reference)
        if cell_row != old:
            raise _error("UNSAFE_REFERENCE", "单元格与所在行不一致", reference)
        cell.set("r", f"{column}{new}")
        formula = cell.find(f"{{{MAIN_NS}}}f")
        if formula is not None and reference not in {"O10", "R10"}:
            _assert_formula_unaffected(formula.text or "", transform, reference)


def _required_rows(parsed: ParsedBundle, rows: Mapping[int, etree._Element]) -> None:
    for number in (*parsed.product_rows, parsed.separator_row, parsed.total_row):
        if number not in rows:
            raise _error("TEMPLATE_VERSION_MISMATCH", "工作表缺少必要行", f"row {number}")


def transform_sheet_xml(
    sheet_xml: bytes,
    parsed: ParsedBundle,
    kept_rows: Sequence[int],
    output_values: Mapping[str, object],
) -> bytes:
    """Return transformed worksheet XML without mutating the parsed snapshot."""

    kept = tuple(kept_rows)
    transform = _RowTransform(parsed, kept)
    root = _xml_root(sheet_xml, "worksheet")
    if root.tag != f"{{{MAIN_NS}}}worksheet":
        raise _error("INVALID_XML", "工作表 XML 根节点无效", "worksheet")
    sheet_data = root.find(f"{{{MAIN_NS}}}sheetData")
    if sheet_data is None:
        raise _error("TEMPLATE_VERSION_MISMATCH", "工作表缺少数据区域", "sheetData")
    source_rows = _row_elements(sheet_data)
    _required_rows(parsed, source_rows)

    new_rows: list[tuple[int, etree._Element]] = []
    for old, original in source_rows.items():
        new = transform.row(old)
        if new is None:
            continue
        moved = copy.deepcopy(original)
        _move_row(moved, old, new, transform)
        new_rows.append((new, moved))
    if len({number for number, _row in new_rows}) != len(new_rows):
        raise _error("UNSAFE_REFERENCE", "行转换产生重复目标", "sheetData")
    new_rows.sort(key=lambda item: item[0])
    for child in list(sheet_data):
        sheet_data.remove(child)
    for _number, row in new_rows:
        sheet_data.append(row)
    rows = {number: row for number, row in new_rows}

    selected_item_list = [
        item
        for shipment in parsed.shipments
        for item in shipment.items
        if item.source_row in kept
    ]
    selected_items = {item.source_row: item for item in selected_item_list}
    if len(selected_items) != len(selected_item_list) or set(selected_items) != set(kept):
        raise _error("INVALID_ROW_SELECTION", "保留行缺少解析数据", "kept_rows")
    totals = compute_totals(selected_items[row].cells for row in kept)
    total_row = transform.row(parsed.total_row)
    if total_row is None:
        raise AssertionError("total row is always mapped")
    total_element = rows[total_row]
    for column in _TOTAL_COLUMNS:
        _write_number(_find_or_create_cell(total_element, f"{column}{total_row}"), totals[column])

    for cell_ref, summary_value in (
        ("G8", totals["K"]),
        ("G9", totals["M"]),
        ("G10", totals["N"]),
    ):
        row_number = _parse_cell_ref(cell_ref, cell_ref)[3]
        if row_number not in rows:
            raise _error("TEMPLATE_VERSION_MISMATCH", "工作表缺少汇总行", cell_ref)
        _write_number(_find_or_create_cell(rows[row_number], cell_ref), summary_value)
    if totals["N"] == 0:
        raise _error("INVALID_TOTAL_INPUT", "体积合计不能为零", "N")
    _write_formula(
        _find_or_create_cell(rows[10], "O10"),
        f"P{total_row}/N{total_row}",
        totals["P"] / totals["N"],
    )
    _write_formula(
        _find_or_create_cell(rows[10], "R10"), "G9/G10", totals["M"] / totals["N"]
    )

    unknown_fields = set(output_values).difference(STANDARD_V1.value_ranges)
    if unknown_fields:
        field = min(unknown_fields)
        raise _error("INVALID_OUTPUT_VALUE", "输出字段不属于 V1 模板", field)
    for field, value in output_values.items():
        top_left = STANDARD_V1.value_ranges[field].split(":", 1)[0]
        row_number = _parse_cell_ref(top_left, top_left)[3]
        if row_number not in rows:
            raise _error("TEMPLATE_VERSION_MISMATCH", "工作表缺少输出字段", top_left)
        _write_inline(_find_or_create_cell(rows[row_number], top_left), value)

    for merge in root.xpath("./main:mergeCells/main:mergeCell", namespaces={"main": MAIN_NS}):
        reference = merge.get("ref")
        if reference is None:
            raise _error("UNSAFE_REFERENCE", "合并区域缺少引用", "mergeCell")
        merge.set("ref", _translate_range(reference, transform, reference))
    for validation in root.xpath(
        "./main:dataValidations/main:dataValidation", namespaces={"main": MAIN_NS}
    ):
        sqref = validation.get("sqref")
        if sqref is None:
            raise _error("UNSAFE_REFERENCE", "数据验证缺少引用", "dataValidation")
        validation.set("sqref", _translate_sqref(sqref, transform, sqref))
        for formula in validation.xpath("./main:formula1 | ./main:formula2", namespaces={"main": MAIN_NS}):
            _assert_formula_unaffected(
                formula.text or "", transform, f"dataValidation {sqref}"
            )
    for hyperlink in root.xpath(
        "./main:hyperlinks/main:hyperlink", namespaces={"main": MAIN_NS}
    ):
        reference = hyperlink.get("ref")
        if reference is None:
            raise _error("UNSAFE_REFERENCE", "超链接缺少引用", "hyperlink")
        hyperlink.set("ref", _translate_range(reference, transform, reference))
    dimension = root.find(f"{{{MAIN_NS}}}dimension")
    if dimension is not None and dimension.get("ref"):
        dimension.set(
            "ref",
            _translate_range(
                dimension.get("ref") or "", transform, "dimension", allow_product_compaction=True
            ),
        )
    return _serialize(root)


def transform_comments_xml(
    comments_xml: bytes,
    parsed: ParsedBundle,
    kept_rows: Sequence[int],
) -> bytes:
    """Move comments using the same row transform; unsafe removed-row comments block."""

    transform = _RowTransform(parsed, tuple(kept_rows))
    root = _xml_root(comments_xml, "comments")
    if root.tag != f"{{{MAIN_NS}}}comments":
        raise _error("INVALID_XML", "批注 XML 根节点无效", "comments")
    for comment in root.xpath("./main:commentList/main:comment", namespaces={"main": MAIN_NS}):
        reference = comment.get("ref")
        if reference is None:
            raise _error("UNSAFE_REFERENCE", "批注缺少引用", "comment")
        comment.set("ref", _translate_cell(reference, transform, reference))
    return _serialize(root)


def transform_drawing_anchors_xml(
    drawing_xml: bytes,
    parsed: ParsedBundle,
    kept_rows: Sequence[int],
    *,
    removed_relationship_ids: frozenset[str] = frozenset(),
) -> bytes:
    """Move supported anchors and drop only explicitly selected removed product pictures."""

    transform = _RowTransform(parsed, tuple(kept_rows))
    root = _xml_root(drawing_xml, "drawing")
    if root.tag != f"{{{XDR_NS}}}wsDr":
        raise _error("INVALID_XML", "绘图 XML 根节点无效", "drawing")
    anchors = root.xpath("./xdr:twoCellAnchor", namespaces={"xdr": XDR_NS})
    if len(anchors) != len(root):
        raise _error("UNSAFE_REFERENCE", "绘图包含未知锚点", "drawing")
    for anchor in anchors:
        embeds = anchor.xpath(
            ".//xdr:blipFill/a:blip/@r:embed",
            namespaces={"xdr": XDR_NS, "a": DRAWING_NS, "r": REL_NS},
        )
        if len(embeds) != 1:
            raise _error("UNSAFE_REFERENCE", "图片关系无法安全转换", "drawing")
        from_row = anchor.find(f"{{{XDR_NS}}}from/{{{XDR_NS}}}row")
        to_row = anchor.find(f"{{{XDR_NS}}}to/{{{XDR_NS}}}row")
        if from_row is None or to_row is None or from_row.text is None or to_row.text is None:
            raise _error("UNSAFE_REFERENCE", "图片锚点缺少行号", embeds[0])
        try:
            old_from = int(from_row.text) + 1
            old_to_boundary = int(to_row.text) + 1
        except ValueError as exc:
            raise _error("UNSAFE_REFERENCE", "图片锚点行号无效", embeds[0]) from exc
        new_from = transform.row(old_from)
        if new_from is None:
            if embeds[0] not in removed_relationship_ids:
                raise _error("UNSAFE_REFERENCE", "图片指向已删除的商品行", embeds[0])
            root.remove(anchor)
            continue
        new_to_boundary = transform.row(old_to_boundary)
        if new_to_boundary is None:
            # A supported one-row image ends at the boundary following its start row.
            if old_to_boundary == old_from + 1:
                new_to_boundary = new_from + 1
            else:
                raise _error("UNSAFE_REFERENCE", "图片范围无法安全转换", embeds[0])
        from_row.text = str(new_from - 1)
        to_row.text = str(new_to_boundary - 1)
    return _serialize(root)


def transform_workbook_xml(
    workbook_xml: bytes,
    parsed: ParsedBundle,
    kept_rows: Sequence[int],
    *,
    sheet_name: str = STANDARD_V1.sheet_name,
) -> bytes:
    """Move configured print areas and force Excel to recalculate transformed formulas."""

    transform = _RowTransform(parsed, tuple(kept_rows))
    root = _xml_root(workbook_xml, "workbook")
    if root.tag != f"{{{MAIN_NS}}}workbook":
        raise _error("INVALID_XML", "工作簿 XML 根节点无效", "workbook")
    for defined_name in root.xpath(
        "./main:definedNames/main:definedName", namespaces={"main": MAIN_NS}
    ):
        formula = defined_name.text or ""
        name = defined_name.get("name") or ""
        if name == "_xlnm.Print_Area":
            prefix, separator, reference = formula.rpartition("!")
            expected = f"'{sheet_name.replace("'", "''")}'"
            if not separator or prefix not in {sheet_name, expected}:
                raise _error("UNSAFE_REFERENCE", "打印区域无法安全转换", name)
            defined_name.text = (
                f"{prefix}!"
                + _translate_range(
                    reference,
                    transform,
                    name,
                    allow_product_compaction=True,
                )
            )
        else:
            _assert_formula_unaffected(formula, transform, name or "definedName")
    calc_pr = root.find(f"{{{MAIN_NS}}}calcPr")
    if calc_pr is None:
        calc_pr = etree.SubElement(root, f"{{{MAIN_NS}}}calcPr")
    calc_pr.set("calcMode", "auto")
    calc_pr.set("fullCalcOnLoad", "1")
    calc_pr.set("forceFullCalc", "1")
    return _serialize(root)
