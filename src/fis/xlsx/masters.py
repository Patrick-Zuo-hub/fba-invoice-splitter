"""Read a master-data workbook into rows ready for an upsert."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from lxml import etree  # type: ignore[import-untyped]

from fis.domain.rules import canonical_amazon_warehouse_code
from fis.xlsx.errors import WorkbookError
from fis.xlsx.package import MAX_ENTRY, SafePackage
from fis.xlsx.parser import _parse_shared_strings, _workbook_relationships
from fis.xlsx.validate import _cell_text

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

FBA_SHEET = "FBA仓代码&详细地址"
CHANNEL_SHEET = "渠道"
DELIVERY_SHEET = "国内交货仓"

_HEADERS: dict[str, tuple[str, ...]] = {
    FBA_SHEET: ("FBA仓代码", "FBA仓详细地址"),
    CHANNEL_SHEET: ("渠道",),
    DELIVERY_SHEET: ("国内交货仓",),
}

_COLUMN = re.compile(r"^([A-Z]+)")


@dataclass(frozen=True, slots=True)
class MasterData:
    """One master-data workbook, in the order the sheets list it."""

    fba_warehouses: tuple[tuple[str, str], ...]
    channels: tuple[str, ...]
    delivery_warehouses: tuple[str, ...]


def _error(code: str, message: str, location: str | None = None) -> WorkbookError:
    return WorkbookError(code, message, location)


def _column_index(reference: str) -> int:
    matched = _COLUMN.match(reference)
    if matched is None:
        raise _error("MASTER_ROW_INCOMPLETE", "单元格坐标无效", reference)
    index = 0
    for character in matched.group(1):
        index = index * 26 + (ord(character) - ord("A") + 1)
    return index - 1


def _sheet_rows(
    package: SafePackage, part: str, shared_strings: tuple[str, ...]
) -> list[list[str]]:
    root = package.read_xml(part, MAX_ENTRY)
    rows: list[list[str]] = []
    for row in root.xpath(".//main:row", namespaces={"main": MAIN_NS}):
        values: list[str] = []
        for cell in row.xpath("./main:c", namespaces={"main": MAIN_NS}):
            position = _column_index(cell.get("r") or "")
            while len(values) <= position:
                values.append("")
            values[position] = _cell_text(cell, shared_strings).strip()
        rows.append(values)
    return rows


def _named_sheets(package: SafePackage) -> dict[str, str]:
    relationships = _workbook_relationships(package)
    targets = {
        relationship.get("Id"): relationship.get("Target") or ""
        for relationship in relationships.xpath(
            "./pkg:Relationship", namespaces={"pkg": PACKAGE_REL_NS}
        )
    }
    workbook = package.read_xml("xl/workbook.xml", MAX_ENTRY)
    parts: dict[str, str] = {}
    for sheet in workbook.xpath("./main:sheets/main:sheet", namespaces={"main": MAIN_NS}):
        name = sheet.get("name") or ""
        target = targets.get(sheet.get(f"{{{OFFICE_REL_NS}}}id") or "")
        if target:
            parts[name] = package.resolve_relationship_target("xl/workbook.xml", target)
    return parts


def _data_rows(rows: list[list[str]], sheet: str) -> list[list[str]]:
    expected = _HEADERS[sheet]
    if not rows:
        raise _error("MASTER_HEADER_MISMATCH", f"{sheet} 缺少表头", sheet)
    header = [value for value in rows[0] if value]
    if tuple(header[: len(expected)]) != expected:
        raise _error(
            "MASTER_HEADER_MISMATCH",
            f"{sheet} 的表头应为 {' / '.join(expected)}",
            sheet,
        )
    return [row for row in rows[1:] if any(value for value in row)]


def _single_column(rows: list[list[str]], sheet: str) -> tuple[str, ...]:
    """Read a name-only sheet, keeping the first of any repeated name.

    The name is the whole of the row here, so a repeat can never disagree with
    itself the way a warehouse address can. Dropping it silently is right.
    """

    seen: set[str] = set()
    values: list[str] = []
    for row in rows:
        value = row[0] if row else ""
        if not value or value.casefold() in seen:
            continue
        seen.add(value.casefold())
        values.append(value)
    return tuple(values)


def _warehouse_rows(rows: list[list[str]]) -> tuple[tuple[str, str], ...]:
    """Collect warehouse rows, tolerating repeats that agree with each other.

    A real master-data sheet repeats a code now and then. Where both rows give
    the same address the repeat carries no information and is dropped; where
    they disagree only a human can say which is current, so the file is
    rejected naming both rows.
    """

    seen: dict[str, tuple[int, str]] = {}
    values: list[tuple[str, str]] = []
    for number, row in enumerate(rows, start=2):
        code = row[0] if row else ""
        address = row[1] if len(row) > 1 else ""
        if not code and not address:
            continue
        if not code or not address:
            raise _error(
                "MASTER_ROW_INCOMPLETE",
                f"{FBA_SHEET} 第 {number} 行缺少仓代码或地址",
                FBA_SHEET,
            )
        # Normalise first: ONT8 and AMAZON-ONT8 are the same warehouse, and
        # letting both through would make the later write order decide.
        normalized = canonical_amazon_warehouse_code(code)
        key = normalized.casefold()
        if key in seen:
            first_row, first_address = seen[key]
            if first_address == address:
                continue
            raise _error(
                "MASTER_DUPLICATE_ROW",
                f"{FBA_SHEET} 中 {normalized} 的地址不一致："
                f"第 {first_row} 行为「{first_address}」，"
                f"第 {number} 行为「{address}」，请先在表格中修正",
                FBA_SHEET,
            )
        seen[key] = (number, address)
        values.append((normalized, address))
    return tuple(values)


def parse_master_data(path: Path | str) -> MasterData:
    """Read every master-data sheet, rejecting the file if any is unusable."""

    try:
        with SafePackage.open(path) as package:
            parts = _named_sheets(package)
            missing = [sheet for sheet in _HEADERS if sheet not in parts]
            if missing:
                raise _error(
                    "MASTER_SHEET_MISSING",
                    "主数据表格缺少工作表：" + "、".join(missing),
                    "、".join(missing),
                )
            shared_strings = _parse_shared_strings(package, _workbook_relationships(package))
            rows = {
                sheet: _data_rows(_sheet_rows(package, parts[sheet], shared_strings), sheet)
                for sheet in _HEADERS
            }
    except WorkbookError:
        raise
    except (OSError, ValueError, etree.XMLSyntaxError) as exc:
        raise _error("MASTER_FILE_UNREADABLE", "无法读取主数据表格") from exc

    return MasterData(
        fba_warehouses=_warehouse_rows(rows[FBA_SHEET]),
        channels=_single_column(rows[CHANNEL_SHEET], CHANNEL_SHEET),
        delivery_warehouses=_single_column(rows[DELIVERY_SHEET], DELIVERY_SHEET),
    )


__all__ = [
    "CHANNEL_SHEET",
    "DELIVERY_SHEET",
    "FBA_SHEET",
    "MasterData",
    "parse_master_data",
]
