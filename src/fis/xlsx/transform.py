"""Pure, deterministic OOXML transformations for one Shipment worksheet."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    Underflow,
)
from typing import cast

from lxml import etree  # type: ignore[import-untyped]

from fis.xlsx.errors import WorkbookError
from fis.xlsx.parser import MAX_EXCEL_COLUMN, MAX_EXCEL_ROW, ParsedBundle
from fis.xlsx.template import STANDARD_V1

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
VML_NS = "urn:schemas-microsoft-com:vml"
EXCEL_NS = "urn:schemas-microsoft-com:office:excel"

_CELL = re.compile(r"^(\$?)([A-Z]{1,3})(\$?)([1-9][0-9]{0,6})$", re.ASCII)
_FORMULA_REFERENCE = re.compile(
    r"(?<![A-Z0-9_.])"
    r"(?:(?P<sheet>'(?:[^']|'')+'|[A-Z_\u0080-\uffff][^!,:()+\-*/\s]*)!)?"
    r"(?P<reference>"
    r"\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}(?::\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6})?"
    r"|\$?[1-9][0-9]{0,6}:\$?[1-9][0-9]{0,6}"
    r"|\$?[A-Z]{1,3}:\$?[A-Z]{1,3})"
    r"(?![A-Z0-9_])",
    re.ASCII | re.IGNORECASE,
)
_SQREF_TOKEN = re.compile(
    r"(?:\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}(?::\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6})?"
    r"|\$?[1-9][0-9]{0,6}:\$?[1-9][0-9]{0,6}|\$?[A-Z]{1,3}:\$?[A-Z]{1,3})",
    re.ASCII | re.IGNORECASE,
)
_TOTAL_COLUMNS = ("K", "M", "N", "P")
_MAX_DECIMAL_TEXT = 128
_MAX_SIGNIFICANT_DIGITS = 15
_MIN_EXCEL_EXPONENT = -307
_MAX_EXCEL_EXPONENT = 307
_ALLOWLISTED_BROKEN_VALIDATION_SQREF = "O4:P4 R5:S5"
_ALLOWLISTED_BROKEN_VALIDATION_ATTRIBUTES = {
    "type": "list",
    "allowBlank": "1",
    "showInputMessage": "1",
    "showErrorMessage": "1",
    "sqref": _ALLOWLISTED_BROKEN_VALIDATION_SQREF,
}
_DYNAMIC_REFERENCE_FUNCTION = re.compile(
    r"(?<![A-Z0-9_.])(?:_XLFN\.)?(?:INDIRECT(?:\.EXT)?|OFFSET)\s*\(",
    re.ASCII | re.IGNORECASE,
)


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
    if not kept_rows or any(
        isinstance(row, bool) or not isinstance(row, int) for row in kept_rows
    ):
        raise _error("INVALID_ROW_SELECTION", "保留行不属于商品区域", "kept_rows")
    if len(set(kept_rows)) != len(kept_rows):
        raise _error("INVALID_ROW_SELECTION", "至少保留一个有效商品行", "kept_rows")
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
        if value.bit_length() > 1024:
            raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column)
        number = Decimal(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped or len(stripped) > _MAX_DECIMAL_TEXT:
            raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column)
        try:
            number = Decimal(stripped)
        except InvalidOperation as exc:
            raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column) from exc
    else:
        # Binary floating point is deliberately not accepted as invoice source data.
        raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column)
    _validate_excel_decimal(number, column)
    return number


def _validate_excel_decimal(number: Decimal, column: str) -> None:
    if not number.is_finite():
        raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column)
    parts = number.as_tuple()
    exponent = cast(int, parts.exponent)
    if (
        len(parts.digits) > _MAX_SIGNIFICANT_DIGITS
        or exponent < _MIN_EXCEL_EXPONENT
        or exponent > _MAX_EXCEL_EXPONENT
        or (
            not number.is_zero()
            and (number.adjusted() < _MIN_EXCEL_EXPONENT or number.adjusted() > _MAX_EXCEL_EXPONENT)
        )
    ):
        raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column)


def compute_totals(rows: Iterable[Mapping[str, object]]) -> dict[str, Decimal]:
    """Sum required invoice columns exactly, rejecting all implicit coercions."""

    values: dict[str, list[Decimal]] = {column: [] for column in _TOTAL_COLUMNS}
    for row in rows:
        for column in _TOTAL_COLUMNS:
            try:
                value = row[column]
            except (KeyError, TypeError) as exc:
                raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column) from exc
            values[column].append(_decimal(value, column))
    result: dict[str, Decimal] = {}
    for column, items in values.items():
        total = _sum_decimals_exact(items)
        _validate_excel_decimal(total, column)
        result[column] = total
    return result


def _sum_decimals_exact(values: Sequence[Decimal]) -> Decimal:
    """Add finite Decimals without consulting or mutating the process context."""

    if not values:
        return Decimal(0)
    exponent = min(cast(int, value.as_tuple().exponent) for value in values)
    total = 0
    for value in values:
        parts = value.as_tuple()
        coefficient = 0
        for digit in parts.digits:
            coefficient = coefficient * 10 + digit
        if parts.sign:
            coefficient = -coefficient
        total += coefficient * 10 ** (cast(int, parts.exponent) - exponent)
    while total and total % 10 == 0:
        total //= 10
        exponent += 1
    sign = 1 if total < 0 else 0
    digits = tuple(int(character) for character in str(abs(total)))
    return Decimal((sign, digits, exponent))


def _divide_for_cache(numerator: Decimal, denominator: Decimal, column: str) -> Decimal:
    """Produce deterministic cached arithmetic independent of global Decimal settings."""

    context = Context(
        prec=_MAX_SIGNIFICANT_DIGITS,
        rounding=ROUND_HALF_EVEN,
        Emin=_MIN_EXCEL_EXPONENT,
        Emax=_MAX_EXCEL_EXPONENT,
        capitals=1,
        clamp=0,
    )
    for signal in context.traps:
        context.traps[signal] = signal in {DivisionByZero, InvalidOperation, Overflow, Underflow}
    context.clear_flags()
    try:
        result = context.divide(numerator, denominator)
    except DecimalException as exc:
        raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column) from exc
    if not result.is_finite() or (
        not result.is_zero()
        and (result.adjusted() < _MIN_EXCEL_EXPONENT or result.adjusted() > _MAX_EXCEL_EXPONENT)
    ):
        raise _error("INVALID_TOTAL_INPUT", "合计字段不是有效数字", column)
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
    if row > MAX_EXCEL_ROW or _column_number(column) > MAX_EXCEL_COLUMN:
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
    if re.fullmatch(r"\$?[A-Z]{1,3}:\$?[A-Z]{1,3}", reference, re.IGNORECASE):
        for column in reference.replace("$", "").split(":"):
            if _column_number(column.upper()) > MAX_EXCEL_COLUMN:
                raise _error("UNSAFE_REFERENCE", "整列引用无法安全转换", location)
        return reference
    row_range = re.fullmatch(
        r"(?P<left_abs>\$?)(?P<left>[1-9][0-9]{0,6}):"
        r"(?P<right_abs>\$?)(?P<right>[1-9][0-9]{0,6})",
        reference,
    )
    if row_range is not None:
        left_row = int(row_range.group("left"))
        right_row = int(row_range.group("right"))
        if left_row > MAX_EXCEL_ROW or right_row > MAX_EXCEL_ROW:
            raise _error("UNSAFE_REFERENCE", "整行引用无法安全转换", location)
        low, high = sorted((left_row, right_row))
        deleted = transform.removed_rows.intersection(range(low, high + 1))
        if deleted and not (
            allow_product_compaction
            and low <= transform.product_rows[0]
            and high >= transform.product_rows[-1]
        ):
            raise _error("UNSAFE_REFERENCE", "整行引用包含已删除的商品行", location)
        new_left = transform.row(left_row)
        new_right = transform.row(right_row)
        if new_left is None:
            new_left = transform.row(transform.kept_rows[0])
        if new_right is None:
            new_right = transform.row(transform.kept_rows[-1])
        if new_left is None or new_right is None:
            raise _error("UNSAFE_REFERENCE", "整行引用无法安全转换", location)
        return (
            f"{row_range.group('left_abs')}{new_left}:"
            f"{row_range.group('right_abs')}{new_right}"
        )
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


def _sheet_name(value: str) -> str:
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def _assert_formula_unaffected(
    formula: str,
    transform: _RowTransform,
    location: str,
    *,
    current_sheet: str = STANDARD_V1.sheet_name,
    unqualified_sheet: str | None = None,
) -> None:
    inspected = _formula_without_strings(formula)
    if _DYNAMIC_REFERENCE_FUNCTION.search(inspected):
        raise _error(
            "UNSAFE_FORMULA_TRANSFORM", "动态公式引用不支持安全行转换", location
        )
    if "[" in inspected or "]" in inspected:
        raise _error("UNSAFE_FORMULA_TRANSFORM", "公式引用结构不受支持", location)
    if re.search(
        r"(?:'(?:[^']|'')+'|[^!,:()+\-*/\s]+)\s*:\s*"
        r"(?:'(?:[^']|'')+'|[^!,:()+\-*/\s]+)\s*!",
        inspected,
    ):
        raise _error("UNSAFE_FORMULA_TRANSFORM", "三维公式引用不受支持", location)
    consumed_bangs: set[int] = set()
    for match in _FORMULA_REFERENCE.finditer(inspected):
        sheet = match.group("sheet")
        if sheet is not None:
            bang = inspected.rfind("!", match.start("sheet"), match.start("reference"))
            if bang < 0:
                raise _error("UNSAFE_FORMULA_TRANSFORM", "公式引用无法完整解析", location)
            consumed_bangs.add(bang)
            if _sheet_name(sheet) != current_sheet:
                continue
        elif unqualified_sheet is not None and unqualified_sheet != current_sheet:
            continue
        reference = match.group("reference").upper()
        if re.fullmatch(r"\$?[A-Z]{1,3}:\$?[A-Z]{1,3}", reference):
            continue
        try:
            translated = _translate_range(reference, transform, location)
        except WorkbookError as exc:
            raise _error(
                "UNSAFE_FORMULA_TRANSFORM", "公式引用受商品行转换影响", location
            ) from exc
        if translated != reference:
            raise _error("UNSAFE_FORMULA_TRANSFORM", "公式引用受商品行转换影响", location)
    if any(character == "!" and index not in consumed_bangs for index, character in enumerate(inspected)):
        raise _error("UNSAFE_FORMULA_TRANSFORM", "公式引用无法完整解析", location)


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite decimal")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _find_cell(row: etree._Element, reference: str) -> etree._Element | None:
    for cell in row.findall(f"{{{MAIN_NS}}}c"):
        if cell.get("r") == reference:
            return cell
    return None


def _column_number(column: str) -> int:
    result = 0
    for char in column:
        result = result * 26 + ord(char) - ord("A") + 1
    return result


def _require_styled_cell(rows: Mapping[int, etree._Element], reference: str) -> etree._Element:
    row_number = _parse_cell_ref(reference, reference)[3]
    row = rows.get(row_number)
    cell = None if row is None else _find_cell(row, reference)
    style = None if cell is None else cell.get("s")
    if cell is None or style is None or not style.isascii() or not style.isdigit():
        raise _error(
            "TEMPLATE_VERSION_MISMATCH", "模板写入目标缺失或样式无效", reference
        )
    return cell


def _validate_formula_target(
    rows: Mapping[int, etree._Element], reference: str, expected_formula: str
) -> None:
    cell = _require_styled_cell(rows, reference)
    formulas = cell.findall(f"{{{MAIN_NS}}}f")
    if (
        len(formulas) != 1
        or formulas[0].attrib
        or "".join((formulas[0].text or "").split()).replace("$", "").upper()
        != expected_formula.upper()
    ):
        raise _error(
            "UNSAFE_FORMULA_TRANSFORM", "模板公式与 V1 基线不一致", reference
        )


def _validate_configured_targets(
    rows: Mapping[int, etree._Element], parsed: ParsedBundle
) -> None:
    for column in _TOTAL_COLUMNS:
        _require_styled_cell(rows, f"{column}{parsed.total_row}")
    for reference in ("G8", "G9", "G10"):
        _require_styled_cell(rows, reference)
    for value_range in STANDARD_V1.value_ranges.values():
        _require_styled_cell(rows, value_range.split(":", 1)[0])
    _validate_formula_target(rows, "O10", f"P{parsed.total_row}/N{parsed.total_row}")
    _validate_formula_target(rows, "R10", "G9/G10")


def _range_rectangle(reference: str) -> tuple[int, int, int, int]:
    parts = reference.split(":")
    if len(parts) != 2:
        raise _error("UNSAFE_REFERENCE", "合并区域引用无效", reference)
    left = _parse_cell_ref(parts[0], reference)
    right = _parse_cell_ref(parts[1], reference)
    return (
        min(_column_number(left[1]), _column_number(right[1])),
        min(left[3], right[3]),
        max(_column_number(left[1]), _column_number(right[1])),
        max(left[3], right[3]),
    )


def _rectangles_overlap(
    left: tuple[int, int, int, int], right: tuple[int, int, int, int]
) -> bool:
    return not (
        left[2] < right[0]
        or right[2] < left[0]
        or left[3] < right[1]
        or right[3] < left[1]
    )


def _validate_configured_merges(root: etree._Element) -> None:
    containers = root.findall(f"{{{MAIN_NS}}}mergeCells")
    if len(containers) != 1:
        raise _error("TEMPLATE_VERSION_MISMATCH", "模板合并区域结构无效", "mergeCells")
    merges = containers[0].findall(f"{{{MAIN_NS}}}mergeCell")
    references = [merge.get("ref") for merge in merges]
    if any(reference is None for reference in references):
        raise _error("UNSAFE_REFERENCE", "合并区域缺少引用", "mergeCell")
    safe_references = cast(list[str], references)
    configured = tuple(STANDARD_V1.value_ranges.values())
    configured_rectangles = {reference: _range_rectangle(reference) for reference in configured}
    for reference in configured:
        if safe_references.count(reference) != 1:
            raise _error(
                "TEMPLATE_VERSION_MISMATCH", "模板值区域必须保持完整合并", reference
            )
    count = containers[0].get("count")
    if count is not None and (
        not count.isascii() or not count.isdigit() or int(count) != len(merges)
    ):
        raise _error("TEMPLATE_VERSION_MISMATCH", "模板合并区域数量无效", "mergeCells")
    for reference in safe_references:
        rectangle = _range_rectangle(reference)
        for configured_reference, configured_rectangle in configured_rectangles.items():
            if reference != configured_reference and _rectangles_overlap(
                rectangle, configured_rectangle
            ):
                raise _error(
                    "TEMPLATE_VERSION_MISMATCH", "模板合并区域存在冲突", reference
                )


def _main(local_name: str) -> str:
    return f"{{{MAIN_NS}}}{local_name}"


_REFERENCE_PATHS: dict[str, tuple[str, tuple[str, ...]]] = {
    _main("dimension"): ("ref", (_main("worksheet"),)),
    _main("mergeCell"): ("ref", (_main("mergeCells"), _main("worksheet"))),
    _main("dataValidation"): (
        "sqref",
        (_main("dataValidations"), _main("worksheet")),
    ),
    _main("hyperlink"): ("ref", (_main("hyperlinks"), _main("worksheet"))),
    _main("conditionalFormatting"): ("sqref", (_main("worksheet"),)),
    _main("autoFilter"): ("ref", (_main("worksheet"),)),
    _main("sortState"): ("ref", (_main("autoFilter"), _main("worksheet"))),
    _main("sortCondition"): (
        "ref",
        (_main("sortState"), _main("autoFilter"), _main("worksheet")),
    ),
    _main("ignoredError"): (
        "sqref",
        (_main("ignoredErrors"), _main("worksheet")),
    ),
    _main("protectedRange"): (
        "sqref",
        (_main("protectedRanges"), _main("worksheet")),
    ),
    _main("selection"): (
        "sqref",
        (_main("sheetView"), _main("sheetViews"), _main("worksheet")),
    ),
}


def _has_parent_path(element: etree._Element, expected: tuple[str, ...]) -> bool:
    parent = element.getparent()
    for tag in expected:
        if parent is None or parent.tag != tag:
            return False
        parent = parent.getparent()
    return True


def _validate_reference_qnames(
    root: etree._Element, processed: set[etree._Element] | None = None
) -> None:
    for element in root.iter():
        for attribute in element.attrib:
            attribute_name = etree.QName(attribute)
            if attribute_name.localname not in {"ref", "sqref"}:
                continue
            contract = _REFERENCE_PATHS.get(element.tag)
            if (
                attribute_name.namespace is not None
                or contract is None
                or attribute_name.localname != contract[0]
                or not _has_parent_path(element, contract[1])
                or (processed is not None and element not in processed)
            ):
                raise _error(
                    "UNSAFE_REFERENCE",
                    "工作表包含未支持的引用结构",
                    etree.QName(element).localname,
                )


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
    if sheet_data.text and sheet_data.text.strip():
        raise _error("UNSAFE_REFERENCE", "工作表数据区域包含不支持的内容", "sheetData")
    result: dict[int, etree._Element] = {}
    for row in sheet_data:
        if not isinstance(row.tag, str) or row.tag != f"{{{MAIN_NS}}}row":
            raise _error("UNSAFE_REFERENCE", "工作表数据区域包含不支持的节点", "sheetData")
        if row.tail and row.tail.strip():
            raise _error("UNSAFE_REFERENCE", "工作表数据区域包含不支持的内容", "sheetData")
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


def _is_allowlisted_broken_validation(validation: etree._Element) -> bool:
    children = list(validation)
    return (
        validation.attrib == _ALLOWLISTED_BROKEN_VALIDATION_ATTRIBUTES
        and len(children) == 1
        and children[0].tag == f"{{{MAIN_NS}}}formula1"
        and not children[0].attrib
        and len(children[0]) == 0
        and children[0].text == "#REF!"
    )


def transform_sheet_xml(
    sheet_xml: bytes,
    parsed: ParsedBundle,
    kept_rows: Sequence[int],
    output_values: Mapping[str, object],
    *,
    sheet_name: str = STANDARD_V1.sheet_name,
) -> bytes:
    """Return transformed worksheet XML without mutating the parsed snapshot."""

    kept = tuple(kept_rows)
    transform = _RowTransform(parsed, kept)
    root = _xml_root(sheet_xml, "worksheet")
    if root.tag != f"{{{MAIN_NS}}}worksheet":
        raise _error("INVALID_XML", "工作表 XML 根节点无效", "worksheet")
    _validate_reference_qnames(root)
    processed_reference_nodes: set[etree._Element] = set()
    sheet_data = root.find(f"{{{MAIN_NS}}}sheetData")
    if sheet_data is None:
        raise _error("TEMPLATE_VERSION_MISMATCH", "工作表缺少数据区域", "sheetData")
    source_rows = _row_elements(sheet_data)
    _required_rows(parsed, source_rows)
    _validate_configured_merges(root)
    _validate_configured_targets(source_rows, parsed)

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
        total_cell = _find_cell(total_element, f"{column}{total_row}")
        if total_cell is None:
            raise AssertionError("validated total cell was lost during row transformation")
        _write_number(total_cell, totals[column])

    for cell_ref, summary_value in (
        ("G8", totals["K"]),
        ("G9", totals["M"]),
        ("G10", totals["N"]),
    ):
        row_number = _parse_cell_ref(cell_ref, cell_ref)[3]
        if row_number not in rows:
            raise _error("TEMPLATE_VERSION_MISMATCH", "工作表缺少汇总行", cell_ref)
        _write_number(_require_styled_cell(rows, cell_ref), summary_value)
    if totals["N"] == 0:
        raise _error("INVALID_TOTAL_INPUT", "体积合计不能为零", "N")
    _write_formula(
        _require_styled_cell(rows, "O10"),
        f"P{total_row}/N{total_row}",
        _divide_for_cache(totals["P"], totals["N"], "N"),
    )
    _write_formula(
        _require_styled_cell(rows, "R10"),
        "G9/G10",
        _divide_for_cache(totals["M"], totals["N"], "N"),
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
        _write_inline(_require_styled_cell(rows, top_left), value)

    for merge in root.xpath("./main:mergeCells/main:mergeCell", namespaces={"main": MAIN_NS}):
        reference = merge.get("ref")
        if reference is None:
            raise _error("UNSAFE_REFERENCE", "合并区域缺少引用", "mergeCell")
        merge.set("ref", _translate_range(reference, transform, reference))
        processed_reference_nodes.add(merge)
    for validation in root.xpath(
        "./main:dataValidations/main:dataValidation", namespaces={"main": MAIN_NS}
    ):
        sqref = validation.get("sqref")
        if sqref is None:
            raise _error("UNSAFE_REFERENCE", "数据验证缺少引用", "dataValidation")
        allowlisted_broken_formula = _is_allowlisted_broken_validation(validation)
        formula_children = list(validation)
        allowed_formula_tags = {f"{{{MAIN_NS}}}formula1", f"{{{MAIN_NS}}}formula2"}
        if (
            any(child.tag not in allowed_formula_tags for child in formula_children)
            or sum(child.tag == f"{{{MAIN_NS}}}formula1" for child in formula_children) > 1
            or sum(child.tag == f"{{{MAIN_NS}}}formula2" for child in formula_children) > 1
        ):
            raise _error("UNSAFE_REFERENCE", "数据验证公式结构不受支持", sqref)
        validation.set("sqref", _translate_sqref(sqref, transform, sqref))
        processed_reference_nodes.add(validation)
        for formula in validation.xpath("./main:formula1 | ./main:formula2", namespaces={"main": MAIN_NS}):
            if allowlisted_broken_formula:
                continue
            _assert_formula_unaffected(
                formula.text or "",
                transform,
                f"dataValidation {sqref}",
                current_sheet=sheet_name,
            )
    for hyperlink in root.xpath(
        "./main:hyperlinks/main:hyperlink", namespaces={"main": MAIN_NS}
    ):
        reference = hyperlink.get("ref")
        if reference is None:
            raise _error("UNSAFE_REFERENCE", "超链接缺少引用", "hyperlink")
        hyperlink.set("ref", _translate_range(reference, transform, reference))
        processed_reference_nodes.add(hyperlink)
    for conditional in root.xpath(
        "./main:conditionalFormatting", namespaces={"main": MAIN_NS}
    ):
        sqref = conditional.get("sqref")
        if sqref is None:
            raise _error("UNSAFE_REFERENCE", "条件格式缺少引用", "conditionalFormatting")
        conditional.set("sqref", _translate_sqref(sqref, transform, sqref))
        processed_reference_nodes.add(conditional)
        for formula in conditional.xpath(
            ".//main:cfRule/main:formula", namespaces={"main": MAIN_NS}
        ):
            _assert_formula_unaffected(
                formula.text or "",
                transform,
                f"conditionalFormatting {sqref}",
                current_sheet=sheet_name,
            )
    for element in root.xpath(
        "./main:autoFilter | .//main:sortState | .//main:sortCondition",
        namespaces={"main": MAIN_NS},
    ):
        reference = element.get("ref")
        if reference is None:
            raise _error("UNSAFE_REFERENCE", "筛选或排序区域缺少引用", "autoFilter")
        element.set(
            "ref",
            _translate_range(
                reference, transform, reference, allow_product_compaction=True
            ),
        )
        processed_reference_nodes.add(element)
    for element in root.xpath(
        "./main:ignoredErrors/main:ignoredError | ./main:protectedRanges/main:protectedRange",
        namespaces={"main": MAIN_NS},
    ):
        sqref = element.get("sqref")
        if sqref is None:
            raise _error("UNSAFE_REFERENCE", "工作表区域缺少引用", "sqref")
        element.set("sqref", _translate_sqref(sqref, transform, sqref))
        processed_reference_nodes.add(element)
    for pane in root.xpath("./main:sheetViews/main:sheetView/main:pane", namespaces={"main": MAIN_NS}):
        top_left = pane.get("topLeftCell")
        if top_left:
            try:
                translated = _translate_cell(top_left, transform, top_left)
            except WorkbookError:
                translated = None
            if translated != top_left:
                pane.attrib.pop("topLeftCell", None)
    for selection in root.xpath(
        "./main:sheetViews/main:sheetView/main:selection", namespaces={"main": MAIN_NS}
    ):
        active = selection.get("activeCell")
        if active:
            try:
                translated_active = _translate_cell(active, transform, active)
            except WorkbookError:
                translated_active = None
            if translated_active != active:
                selection.attrib.pop("activeCell", None)
        sqref = selection.get("sqref")
        if sqref:
            processed_reference_nodes.add(selection)
            try:
                translated_sqref = _translate_sqref(sqref, transform, sqref)
            except WorkbookError:
                translated_sqref = None
            if translated_sqref != sqref:
                selection.attrib.pop("sqref", None)
    dimension = root.find(f"{{{MAIN_NS}}}dimension")
    if dimension is not None and dimension.get("ref"):
        processed_reference_nodes.add(dimension)
        dimension.set(
            "ref",
            _translate_range(
                dimension.get("ref") or "", transform, "dimension", allow_product_compaction=True
            ),
        )
    _validate_reference_qnames(root, processed_reference_nodes)
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
    processed: set[etree._Element] = set()
    _validate_comment_reference_nodes(root)
    for comment in root.xpath("./main:commentList/main:comment", namespaces={"main": MAIN_NS}):
        reference = comment.get("ref")
        if reference is None:
            raise _error("UNSAFE_REFERENCE", "批注缺少引用", "comment")
        comment.set("ref", _translate_cell(reference, transform, reference))
        processed.add(comment)
    _validate_comment_reference_nodes(root, processed)
    return _serialize(root)


def _validate_comment_reference_nodes(
    root: etree._Element, processed: set[etree._Element] | None = None
) -> None:
    """Ensure every comment reference has the exact supported QName and parent path."""

    for element in root.iter():
        for attribute in element.attrib:
            attribute_name = etree.QName(attribute)
            if attribute_name.localname != "ref":
                continue
            parent = element.getparent()
            grandparent = parent.getparent() if parent is not None else None
            supported = (
                attribute_name.namespace is None
                and element.tag == f"{{{MAIN_NS}}}comment"
                and parent is not None
                and parent.tag == f"{{{MAIN_NS}}}commentList"
                and grandparent is root
                and (processed is None or element in processed)
            )
            if not supported:
                raise _error("UNSAFE_REFERENCE", "批注引用结构不受支持", "comments")


def transform_comments_vml_xml(
    vml_xml: bytes,
    parsed: ParsedBundle,
    kept_rows: Sequence[int],
    *,
    comment_references: Sequence[str],
) -> bytes:
    """Move legacy note VML rows and verify each shape against its comment cell."""

    transform = _RowTransform(parsed, tuple(kept_rows))
    root = _xml_root(vml_xml, "comments-vml")
    root_name = etree.QName(root)
    if root_name.localname != "xml" or root_name.namespace not in {None, VML_NS}:
        raise _error("INVALID_XML", "批注 VML 根节点无效", "comments-vml")
    clients = root.xpath(".//x:ClientData", namespaces={"x": EXCEL_NS})
    if len(clients) != len(comment_references):
        raise _error("UNSAFE_REFERENCE", "批注与 VML 形状数量不一致", "comments-vml")
    expected: dict[tuple[int, int], str] = {}
    for reference in comment_references:
        _ca, column, _ra, old_row = _parse_cell_ref(reference, reference)
        coordinate = (old_row - 1, _column_number(column) - 1)
        if coordinate in expected:
            raise _error("UNSAFE_REFERENCE", "批注引用重复", reference)
        expected[coordinate] = reference
    seen: set[tuple[int, int]] = set()
    for client in clients:
        if client.get("ObjectType") != "Note":
            raise _error("UNSAFE_REFERENCE", "VML 包含非批注对象", "comments-vml")
        parent = client.getparent()
        if parent is None or parent.tag != f"{{{VML_NS}}}shape":
            raise _error("UNSAFE_REFERENCE", "VML 批注形状结构无效", "comments-vml")
        row_nodes = client.findall(f"{{{EXCEL_NS}}}Row")
        column_nodes = client.findall(f"{{{EXCEL_NS}}}Column")
        if len(row_nodes) != 1 or len(column_nodes) != 1:
            raise _error("UNSAFE_REFERENCE", "VML 批注缺少行列定位", "comments-vml")
        try:
            vml_row = int(row_nodes[0].text or "")
            vml_column = int(column_nodes[0].text or "")
        except ValueError as exc:
            raise _error("UNSAFE_REFERENCE", "VML 批注行列定位无效", "comments-vml") from exc
        coordinate = (vml_row, vml_column)
        matched_reference = expected.get(coordinate)
        if matched_reference is None or coordinate in seen:
            raise _error("UNSAFE_REFERENCE", "批注与 VML 行列不一致", "comments-vml")
        seen.add(coordinate)
        old_row = vml_row + 1
        new_row = transform.row(old_row)
        if new_row is None:
            raise _error(
                "UNSAFE_REFERENCE", "批注指向已删除的商品行", matched_reference
            )
        row_nodes[0].text = str(new_row - 1)
    if seen != set(expected):
        raise _error("UNSAFE_REFERENCE", "批注与 VML 行列不一致", "comments-vml")
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


def _split_print_areas(formula: str, location: str) -> tuple[str, ...]:
    areas: list[str] = []
    start = 0
    index = 0
    quoted = False
    while index < len(formula):
        character = formula[index]
        if character == "'":
            if quoted and index + 1 < len(formula) and formula[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif character == "," and not quoted:
            area = formula[start:index].strip()
            if not area:
                raise _error("UNSAFE_REFERENCE", "打印区域无法安全转换", location)
            areas.append(area)
            start = index + 1
        index += 1
    if quoted:
        raise _error("UNSAFE_REFERENCE", "打印区域无法安全转换", location)
    area = formula[start:].strip()
    if not area:
        raise _error("UNSAFE_REFERENCE", "打印区域无法安全转换", location)
    areas.append(area)
    return tuple(areas)


def _split_sheet_qualified_reference(
    area: str, location: str
) -> tuple[str | None, str]:
    if "[" in area or "]" in area:
        raise _error("UNSAFE_REFERENCE", "外部工作簿引用不受支持", location)
    quoted = False
    separator = -1
    index = 0
    while index < len(area):
        character = area[index]
        if character == "'":
            if quoted and index + 1 < len(area) and area[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif character == "!" and not quoted:
            separator = index
        index += 1
    if quoted:
        raise _error("UNSAFE_REFERENCE", "工作表限定引用无效", location)
    if separator < 0:
        return None, area
    prefix = area[:separator]
    reference = area[separator + 1 :]
    if not prefix or not reference:
        raise _error("UNSAFE_REFERENCE", "工作表限定引用无效", location)
    if ":" in prefix:
        raise _error("UNSAFE_REFERENCE", "三维工作表引用不受支持", location)
    return prefix, reference


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
    sheets = root.xpath("./main:sheets/main:sheet", namespaces={"main": MAIN_NS})
    sheet_names = [sheet.get("name") for sheet in sheets]
    if any(not name for name in sheet_names) or len(set(sheet_names)) != len(sheet_names):
        raise _error("UNSAFE_REFERENCE", "工作簿工作表顺序无效", "sheets")
    target_indices = [index for index, name in enumerate(sheet_names) if name == sheet_name]
    if sheets and len(target_indices) != 1:
        raise _error("UNSAFE_REFERENCE", "目标工作表无法唯一识别", sheet_name)
    target_index = target_indices[0] if target_indices else None

    for defined_name in root.xpath(
        "./main:definedNames/main:definedName", namespaces={"main": MAIN_NS}
    ):
        formula = defined_name.text or ""
        name = defined_name.get("name") or ""
        local_text = defined_name.get("localSheetId")
        local_index: int | None = None
        if local_text is not None:
            if not local_text.isascii() or not local_text.isdigit() or not sheets:
                raise _error("UNSAFE_REFERENCE", "名称作用域无效", name or "definedName")
            local_index = int(local_text)
            if local_index >= len(sheets):
                raise _error("UNSAFE_REFERENCE", "名称作用域无效", name or "definedName")
        if name == "_xlnm.Print_Area":
            transformed: list[str] = []
            for area in _split_print_areas(formula, name):
                prefix, reference = _split_sheet_qualified_reference(area, name)
                applies_to_target = prefix is not None and _sheet_name(prefix) == sheet_name
                if prefix is None:
                    if local_index is None or target_index is None:
                        raise _error("UNSAFE_REFERENCE", "打印区域缺少工作表限定", name)
                    applies_to_target = local_index == target_index
                if not applies_to_target:
                    transformed.append(area)
                    continue
                translated = _translate_range(
                    reference, transform, name, allow_product_compaction=True
                )
                transformed.append(f"{prefix}!{translated}" if prefix is not None else translated)
            defined_name.text = ",".join(transformed)
        else:
            _assert_formula_unaffected(
                formula,
                transform,
                name or "definedName",
                current_sheet=sheet_name,
                unqualified_sheet=(
                    cast(str, sheet_names[local_index])
                    if local_index is not None
                    else sheet_name
                ),
            )
    calc_pr = root.find(f"{{{MAIN_NS}}}calcPr")
    if calc_pr is None:
        calc_pr = etree.SubElement(root, f"{{{MAIN_NS}}}calcPr")
    calc_pr.set("calcMode", "auto")
    calc_pr.set("fullCalcOnLoad", "1")
    calc_pr.set("forceFullCalc", "1")
    return _serialize(root)
