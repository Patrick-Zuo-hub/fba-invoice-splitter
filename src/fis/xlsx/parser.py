"""Parse the supported FIS v1 invoice worksheet without evaluating formulas."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from posixpath import normpath
from types import MappingProxyType

from lxml import etree  # type: ignore[import-untyped]

from fis.xlsx.errors import WorkbookError
from fis.xlsx.package import MAX_ENTRY, SafePackage
from fis.xlsx.template import STANDARD_V1

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MAX_WORKSHEET_ROWS = 100_000
_COLUMNS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_REQUIRED_HEADERS = (
    "货件编号SHIPMENT ID\n(亚马逊货物必填）\n不可合并",
    "跟踪编号\nREFERENCE ID\n(亚马逊货物必填）\n不可合并",
    "图片\nPRODUCT PICTURE\n(必填)",
)


@dataclass(frozen=True, slots=True)
class ParsedItem:
    shipment_id: str
    source_row: int
    cells: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cells", MappingProxyType(dict(self.cells)))


@dataclass(frozen=True, slots=True)
class ParsedShipment:
    shipment_id: str
    items: tuple[ParsedItem, ...]


@dataclass(frozen=True, slots=True)
class ParsedBundle:
    shipments: tuple[ParsedShipment, ...]
    product_rows: tuple[int, ...]
    separator_row: int
    total_row: int


def _template_error(location: str) -> WorkbookError:
    return WorkbookError("TEMPLATE_VERSION_MISMATCH", "工作簿不符合 FIS V1 模板", location)


def _normalized_header(value: str) -> str:
    return "".join(value.replace("（", "(").replace("）", ")").split())


def _cell_column_and_row(reference: str) -> tuple[str, int] | None:
    if not reference:
        return None
    column = ""
    digits = ""
    for character in reference:
        if character.isalpha() and not digits:
            column += character.upper()
        elif character.isdigit():
            digits += character
        else:
            return None
    if not column.isascii() or not digits or int(digits) < 1:
        return None
    return column, int(digits)


def _text(element: etree._Element) -> str:
    return "".join(element.xpath(".//main:t/text()", namespaces={"main": MAIN_NS}))


def _parse_shared_strings(package: SafePackage) -> tuple[str, ...]:
    if "xl/sharedStrings.xml" not in package.names:
        return ()
    root = package.read_xml("xl/sharedStrings.xml", MAX_ENTRY)
    return tuple(_text(item) for item in root.xpath("./main:si", namespaces={"main": MAIN_NS}))


def _cell_value(cell: etree._Element, shared_strings: tuple[str, ...], location: str) -> str:
    formula = cell.find(f"{{{MAIN_NS}}}f")
    if formula is not None:
        return f"={formula.text or ''}"
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        inline = cell.find(f"{{{MAIN_NS}}}is")
        return "" if inline is None else _text(inline)
    value = cell.findtext(f"{{{MAIN_NS}}}v") or ""
    if cell_type == "s":
        try:
            index = int(value)
        except ValueError as exc:
            raise _template_error(location) from exc
        if index < 0 or index >= len(shared_strings):
            raise _template_error(location)
        return shared_strings[index]
    if cell_type in {None, "n", "b", "e", "str"}:
        return value
    raise _template_error(location)


def _worksheet_part(package: SafePackage) -> str:
    workbook = package.read_xml("xl/workbook.xml", MAX_ENTRY)
    sheets = workbook.xpath("./main:sheets/main:sheet", namespaces={"main": MAIN_NS})
    matched = [sheet for sheet in sheets if sheet.get("name") == STANDARD_V1.sheet_name]
    if len(matched) != 1:
        raise _template_error("xl/workbook.xml")
    relation_id = matched[0].get(f"{{{OFFICE_REL_NS}}}id")
    if not relation_id:
        raise _template_error("xl/workbook.xml")

    relationships = package.read_xml("xl/_rels/workbook.xml.rels", MAX_ENTRY)
    matching_ids = [
        relation
        for relation in relationships.xpath(
            "./pkg:Relationship", namespaces={"pkg": PACKAGE_REL_NS}
        )
        if relation.get("Id") == relation_id
    ]
    if len(matching_ids) != 1 or not matching_ids[0].get("Type", "").endswith("/worksheet"):
        raise _template_error("xl/_rels/workbook.xml.rels")
    target = matching_ids[0].get("Target")
    if not target:
        raise _template_error("xl/_rels/workbook.xml.rels")
    part = normpath(target.lstrip("/") if target.startswith("/") else f"xl/{target}")
    if part in {"", "."} or part.startswith("../"):
        raise _template_error("xl/_rels/workbook.xml.rels")
    return part


def _rows_from_worksheet(
    root: etree._Element, shared_strings: tuple[str, ...]
) -> tuple[dict[int, dict[str, str]], int]:
    rows: dict[int, dict[str, str]] = {}
    max_row = 0
    for row in root.xpath("./main:sheetData/main:row", namespaces={"main": MAIN_NS}):
        try:
            row_number = int(row.get("r", ""))
        except ValueError as exc:
            raise _template_error("sheetData") from exc
        if row_number < 1 or row_number > MAX_WORKSHEET_ROWS or row_number in rows:
            raise _template_error(f"A{row_number if row_number > 0 else 1}")
        values: dict[str, str] = {}
        for cell in row.xpath("./main:c", namespaces={"main": MAIN_NS}):
            parsed = _cell_column_and_row(cell.get("r", ""))
            if parsed is None:
                raise _template_error(f"A{row_number}")
            column, cell_row = parsed
            if cell_row != row_number:
                raise _template_error(cell.get("r", f"A{row_number}"))
            if column not in _COLUMNS:
                continue
            if column in values:
                raise _template_error(cell.get("r", f"{column}{row_number}"))
            values[column] = _cell_value(cell, shared_strings, f"{column}{row_number}")
        rows[row_number] = values
        max_row = max(max_row, row_number)
    return rows, max_row


def _validate_headers(rows: Mapping[int, Mapping[str, str]]) -> None:
    headers = rows.get(11, {})
    for column, expected in zip("ABC", _REQUIRED_HEADERS, strict=True):
        if _normalized_header(headers.get(column, "")) != _normalized_header(expected):
            raise _template_error(f"{column}11")


def _is_empty(value: str) -> bool:
    return not value.strip()


def _validate_separator(values: Mapping[str, str], row_number: int) -> None:
    for column in _COLUMNS:
        if not _is_empty(values.get(column, "")):
            raise _template_error(f"{column}{row_number}")


def _is_total(value: str) -> bool:
    return "".join(value.split()).casefold() == "合计total".casefold()


def parse_invoice_bundle(path: Path | str) -> ParsedBundle:
    """Return the immutable product rows from one validated FIS V1 workbook."""

    with SafePackage.open(path) as package:
        worksheet = package.read_xml(_worksheet_part(package), MAX_ENTRY)
        rows, max_row = _rows_from_worksheet(worksheet, _parse_shared_strings(package))

    _validate_headers(rows)
    start = STANDARD_V1.product_start_row
    if max_row < start:
        raise _template_error(f"A{start}")

    grouped: dict[str, list[ParsedItem]] = {}
    product_rows: list[int] = []
    for row_number in range(start, min(max_row, MAX_WORKSHEET_ROWS) + 1):
        values = rows.get(row_number, {})
        shipment_id = values.get("A", "")
        reference_id = values.get("B", "")
        shipment_empty = _is_empty(shipment_id)
        reference_empty = _is_empty(reference_id)
        if shipment_empty and reference_empty:
            _validate_separator(values, row_number)
            if not product_rows:
                raise _template_error(f"A{row_number}")
            total_row = row_number + 1
            total_values = rows.get(total_row, {})
            if not _is_total(total_values.get("A", "")):
                raise _template_error(f"A{total_row}")
            shipments = tuple(
                ParsedShipment(shipment_id=identifier, items=tuple(items))
                for identifier, items in grouped.items()
            )
            return ParsedBundle(
                shipments=shipments,
                product_rows=tuple(product_rows),
                separator_row=row_number,
                total_row=total_row,
            )
        if shipment_empty:
            raise _template_error(f"A{row_number}")
        if reference_empty:
            raise _template_error(f"B{row_number}")
        cells = {column: values.get(column, "") for column in _COLUMNS}
        item = ParsedItem(shipment_id=shipment_id.strip(), source_row=row_number, cells=cells)
        grouped.setdefault(item.shipment_id, []).append(item)
        product_rows.append(row_number)

    raise _template_error(f"A{max_row + 1}")
