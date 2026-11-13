"""Scalar-only adapter for Equator's 50-column, one-data-sheet workbook."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, cast
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree  # type: ignore[import-untyped]

from fis.xlsx.combined import next_output_path
from fis.xlsx.drawings import _validate_media
from fis.xlsx.errors import WorkbookError
from fis.xlsx.package import MAX_ENTRY, SafePackage
from fis.xlsx.parser import (
    MAIN_NS,
    OFFICE_REL_NS,
    PACKAGE_REL_NS,
    ParsedBundle,
    ParsedItem,
    ParsedShipment,
    _cell_column_and_row,
    _cell_value,
    _normalized_header,
    _parse_shared_strings,
    _validate_workbook_sheets,
    _workbook_relationships,
    parse_invoice_bundle,
)

NS = {"s": MAIN_NS}
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
NUMERIC = frozenset({"X", "Y", "Z", "AA", "AB", "AJ", "AK", "AL", "AM", "AU"})
REBUILT = frozenset("ABCDEFGHIJKLMN OPQRS".replace(" ", "")) | {"W"}
FORMATS = ("FBA货箱编号", "PONO码", "中文品名")


@lru_cache(maxsize=1)
def default_catalog() -> dict[str, Any]:
    return json.loads((Path(__file__).parent / "assets/equator-catalog.json").read_text("utf-8"))  # type: ignore[no-any-return]


def _error(message: str, location: str | None = None) -> WorkbookError:
    return WorkbookError("EQUATOR_INVALID", f"{message}（{location}）" if location else message, location)


def _xml(root: etree._Element) -> bytes:
    return cast(bytes, etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True))


def _sheet(package: SafePackage, name: str) -> tuple[str, etree._Element] | None:
    relationships = _workbook_relationships(package)
    sheets = package.read_xml("xl/workbook.xml", MAX_ENTRY).findall(f"{{{MAIN_NS}}}sheets/{{{MAIN_NS}}}sheet")
    matched = [s for s in sheets if s.get("name") == name]
    if not matched:
        return None
    validated = _validate_workbook_sheets(package, sheets, relationships)
    return validated[matched[0].get(f"{{{OFFICE_REL_NS}}}id")]


def _cells(
    package: SafePackage, sheet: etree._Element,
) -> dict[int, dict[str, str]]:
    strings = _parse_shared_strings(package, _workbook_relationships(package))
    result: dict[int, dict[str, str]] = {}
    for row in sheet.findall(f"{{{MAIN_NS}}}sheetData/{{{MAIN_NS}}}row"):
        try:
            number = int(row.get("r", "0"))
        except ValueError as error:
            raise _error("行号无效", row.get("r")) from error
        if not 1 <= number <= 100_000 or number in result:
            raise _error("行号无效或重复", row.get("r"))
        values: dict[str, str] = {}
        for c in row.findall(f"{{{MAIN_NS}}}c"):
            position = _cell_column_and_row(c.get("r", ""))
            if position is None or position[1] != number or position[0] in values:
                raise _error("单元格位置无效或重复", c.get("r"))
            column = position[0]
            if number >= 5 and c.get("t") == "e" and column not in REBUILT:
                raise _error("商品单元格包含 Excel 错误，请修正源文件", c.get("r"))
            formula = c.find(f"{{{MAIN_NS}}}f")
            if formula is not None:
                value = c.findtext(f"{{{MAIN_NS}}}v") or ""
                # Orders and addresses are explicitly rebuilt. Formula results
                # in product data need a real cached scalar, except the two
                # totals whose arithmetic is defined below.
                if not value and column not in REBUILT | {"AK", "AM"}:
                    raise _error("商品公式缺少结果，请在领星导出值或在 Excel 计算后保存", c.get("r"))
                values[column] = value
            else:
                values[column] = _cell_value(c, strings, c.get("r", ""))
        result[number] = values
    return result


def parse_supported_invoice(path: Path) -> ParsedBundle:
    with SafePackage.open(path, allow_external_workbook_links=True) as package:
        found = _sheet(package, "模版")
        if found is not None:
            return parse_equator_package(package, found[1])
    # Reopen strictly: stale workbook links are not enabled for the old engine.
    return parse_invoice_bundle(path)


def parse_equator_package(package: SafePackage, sheet: etree._Element) -> ParsedBundle:
    rows = _cells(package, sheet)
    headers = rows.get(4, {})
    for column, expected in default_catalog()["headers"].items():
        if _normalized_header(headers.get(column, "")) != _normalized_header(expected):
            raise _error("赤道 2.0 表头不匹配，请使用原始 50 列模板", f"{column}4")
    grouped: dict[str, list[ParsedItem]] = {}
    product_rows: list[int] = []
    carry: dict[str, str] = {}
    merges: dict[int, str] = {}
    for merge in sheet.findall(f"{{{MAIN_NS}}}mergeCells/{{{MAIN_NS}}}mergeCell"):
        ref = merge.get("ref", "")
        match = re.fullmatch(r"X([0-9]+):X([0-9]+)", ref)
        if match:
            start, end = map(int, match.groups())
            for n in range(start, end + 1):
                merges[n] = rows.get(start, {}).get("X", "")
        elif any(int(x) >= 5 for x in re.findall(r"[A-Z]+([0-9]+)", ref)):
            raise _error("明细区仅支持箱数 X 列纵向合并，请勿合并货件号或其他商品字段", ref)
    for number, values in sorted(rows.items()):
        if number < 5:
            continue
        if not any(values.get(c, "").strip() for c in ("U", "V", "AC", "AD", "AJ", "AL")):
            continue
        data = dict(values)
        shipment = data.get("U", "").strip().upper()
        if not re.fullmatch(r"FBA[A-Z0-9]{9}(?:[A-Z0-9]{7})?", shipment):
            raise _error("FBA 货箱编号应为 12 位主单或 19 位子单", f"U{number}")
        shipment = shipment[:12]
        if not re.fullmatch(r"[A-Z0-9]{8}", data.get("V", "").strip()):
            raise _error("PONO 码应为 8 位大写字母或数字", f"V{number}")
        for column in REBUILT - {"W"}:
            if data.get(column):
                carry[column] = data[column]
            else:
                data[column] = carry.get(column, "")
        for column in ("AC", "AD", "AE", "AF", "AN", "AO", "AP", "AQ"):
            if not data.get(column, "").strip():
                raise _error("缺少必填商品字段", f"{column}{number}")
        numbers: dict[str, Decimal] = {}
        for column in ("X", "Y", "Z", "AA", "AB", "AJ", "AL"):
            raw = merges.get(number, "") if column == "X" and not data.get("X") else data.get(column, "")
            try:
                value = Decimal(raw)
                if not value.is_finite() or value <= 0:
                    raise InvalidOperation
            except InvalidOperation as exc:
                raise _error("箱规、数量及单价必须为有效正数", f"{column}{number}") from exc
            if column in {"X", "AJ"} and value != value.to_integral_value():
                raise _error("箱数和单箱件数必须为整数", f"{column}{number}")
            numbers[column] = value
        for column, expected in (("AK", numbers["X"] * numbers["AJ"]), ("AM", numbers["X"] * numbers["AJ"] * numbers["AL"])):
            if data.get(column):
                try:
                    actual = Decimal(data[column])
                except InvalidOperation as exc:
                    raise _error("申报合计不是数字", f"{column}{number}") from exc
                if not actual.is_finite() or abs(actual - expected) > Decimal("0.01"):
                    raise _error("申报合计与箱数、单箱件数、单价不一致", f"{column}{number}")
            data[column] = str(expected)
        grouped.setdefault(shipment, []).append(ParsedItem(shipment, number, data))
        product_rows.append(number)
    if not product_rows:
        raise _error("未找到商品明细，请从第 5 行填写")
    return ParsedBundle(
        tuple(ParsedShipment(k, tuple(v)) for k, v in grouped.items()),
        tuple(product_rows), max(product_rows) + 1, max(product_rows) + 2, "equator-v2",
    )


def _cell(row: etree._Element, column: str, value: str, style: str | None = None) -> None:
    cell = etree.SubElement(row, f"{{{MAIN_NS}}}c", r=f"{column}{row.get('r')}")
    if style:
        cell.set("s", style)
    if value == "":
        return
    if column in NUMERIC or column == "F":
        etree.SubElement(cell, f"{{{MAIN_NS}}}v").text = value
    else:
        cell.set("t", "inlineStr")
        etree.SubElement(etree.SubElement(cell, f"{{{MAIN_NS}}}is"), f"{{{MAIN_NS}}}t").text = value


def _reference_sheet(rows: list[list[str]]) -> bytes:
    sheet = etree.Element(f"{{{MAIN_NS}}}worksheet", nsmap={None: MAIN_NS})
    data = etree.SubElement(sheet, f"{{{MAIN_NS}}}sheetData")
    for n, values in enumerate(rows, 1):
        row = etree.SubElement(data, f"{{{MAIN_NS}}}row", r=str(n))
        for i, value in enumerate(values):
            _cell(row, chr(65 + i), value)
    return _xml(sheet)


def generate_equator(
    source: Path, bundle: ParsedBundle, fields: Mapping[str, Mapping[str, str]],
    destination: Path, filename: str, catalog: Mapping[str, Any],
) -> Path:
    """Build a new package; external links, formulas and stale rows never escape."""
    output = next_output_path(destination, filename)
    rows = sorted((item for shipment in bundle.shipments for item in shipment.items), key=lambda i: i.source_row)
    row_map = {item.source_row: n for n, item in enumerate(rows, 5)}
    parts: dict[str, bytes] = {}
    image_hashes: dict[str, str] = {}
    with SafePackage.open(source, allow_external_workbook_links=True) as package:
        found = _sheet(package, "模版")
        assert found is not None
        sheet_part, sheet = found
        original = {int(r.get("r")): r for r in sheet.findall(f"{{{MAIN_NS}}}sheetData/{{{MAIN_NS}}}row")}
        sheet_data = sheet.find(f"{{{MAIN_NS}}}sheetData")
        assert sheet_data is not None
        for row in list(sheet_data):
            if int(row.get("r")) >= 5:
                sheet_data.remove(row)
        styles = package.read_xml("xl/styles.xml", MAX_ENTRY)
        formats = styles.find(f"{{{MAIN_NS}}}numFmts")
        if formats is None:
            formats = etree.Element(f"{{{MAIN_NS}}}numFmts", count="0")
            styles.insert(0, formats)
        format_id = max([163] + [int(f.get("numFmtId")) for f in formats]) + 1
        etree.SubElement(formats, f"{{{MAIN_NS}}}numFmt", numFmtId=str(format_id), formatCode="yyyy-mm-dd hh:mm")
        formats.set("count", str(len(formats)))
        xfs = styles.find(f"{{{MAIN_NS}}}cellXfs")
        assert xfs is not None
        date_style = str(len(xfs))
        xf = copy.deepcopy(xfs[0])
        xf.set("numFmtId", str(format_id))
        xf.set("applyNumberFormat", "1")
        xfs.append(xf)
        xfs.set("count", str(len(xfs)))
        parts["xl/styles.xml"] = _xml(styles)
        for item in rows:
            current = fields[item.shipment_id]
            values = dict(item.cells)
            arrival = datetime.fromisoformat(current["arrival_time"])
            excel_date = Decimal(str((arrival - datetime(1899, 12, 30, tzinfo=UTC).replace(tzinfo=None)).total_seconds())) / Decimal(86400)
            values.update({
                "A": current["customer_code"], "B": item.shipment_id,
                "C": current["remarks"], "D": current["goods_material"],
                "E": current["delivery_warehouse"], "F": str(excel_date),
                "G": current["channel_code"], "H": current["channel"], "I": "FBA仓",
                "J": current["warehouse_code"], "W": current["customs"],
            })
            for column in "KLMNOPQRS":
                values[column] = current.get(f"address_{column}", "")
            origin = original[item.source_row]
            new_row = etree.SubElement(sheet_data, f"{{{MAIN_NS}}}row", **{**origin.attrib, "r": str(row_map[item.source_row])})
            cell_styles = {re.sub(r"\d", "", c.get("r")): c.get("s") for c in origin}
            for column in catalog["headers"]:
                _cell(new_row, column, values.get(column, ""), date_style if column == "F" else cell_styles.get(column))
        # Keep only the title merges and supported shared-carton merges.
        merges = sheet.find(f"{{{MAIN_NS}}}mergeCells")
        if merges is not None:
            for merge in list(merges):
                ref = merge.get("ref", "")
                m = re.fullmatch(r"X([0-9]+):X([0-9]+)", ref)
                if m:
                    start, end = map(int, m.groups())
                    if not all(n in row_map for n in range(start, end + 1)):
                        raise _error("合并箱数覆盖非商品行", ref)
                    merge.set("ref", f"X{row_map[start]}:X{row_map[end]}")
                elif any(int(n) >= 5 for n in re.findall(r"[A-Z]+([0-9]+)", ref)):
                    merges.remove(merge)
            merges.set("count", str(len(merges)))
        for tag in ("dataValidations", "extLst", "legacyDrawing", "autoFilter", "conditionalFormatting"):
            for node in sheet.findall(f"{{{MAIN_NS}}}{tag}"):
                sheet.remove(node)
        dimension = sheet.find(f"{{{MAIN_NS}}}dimension")
        if dimension is not None:
            dimension.set("ref", f"A1:AX{len(rows)+4}")
        # Only anchored product images are carried into the new package.
        drawing_node = sheet.find(f"{{{MAIN_NS}}}drawing")
        image_rows: set[int] = set()
        if drawing_node is not None:
            source_rels = str(PurePosixPath(sheet_part).parent / "_rels" / (PurePosixPath(sheet_part).name + ".rels"))
            rels = package.read_xml(source_rels, MAX_ENTRY)
            rel = next((r for r in rels if r.get("Id") == drawing_node.get(f"{{{OFFICE_REL_NS}}}id")), None)
            if rel is None or not rel.get("Type", "").endswith("/drawing"):
                raise _error("商品图片关系无效")
            drawing_part = package.resolve_relationship_target(sheet_part, rel.get("Target"))
            drawing = package.read_xml(drawing_part, MAX_ENTRY)
            drawing_rels = str(PurePosixPath(drawing_part).parent / "_rels" / (PurePosixPath(drawing_part).name + ".rels"))
            links = package.read_xml(drawing_rels, MAX_ENTRY)
            used: set[str] = set()
            for anchor in list(drawing):
                start = anchor.find(f"{{{XDR}}}from")
                end = anchor.find(f"{{{XDR}}}to")
                if start is None or end is None or anchor.find(f"{{{XDR}}}pic") is None:
                    raise _error("仅支持锚定到 T 列单元格的商品图片")
                source_row = int(start.findtext(f"{{{XDR}}}row")) + 1
                if source_row not in row_map:
                    drawing.remove(anchor)
                    continue
                if int(start.findtext(f"{{{XDR}}}col")) != 19:
                    raise _error("商品图片必须位于 T 列", str(source_row))
                if (
                    int(end.findtext(f"{{{XDR}}}row")) > source_row
                    or int(end.findtext(f"{{{XDR}}}col")) > 20
                ):
                    raise _error("商品图片跨越多条明细，无法确定归属", str(source_row))
                delta = row_map[source_row] - source_row
                for marker in (start, end):
                    node = marker.find(f"{{{XDR}}}row")
                    node.text = str(int(node.text) + delta)
                image_rows.add(source_row)
                blips = anchor.findall(f".//{{{A}}}blip")
                if len(blips) != 1 or not blips[0].get(f"{{{OFFICE_REL_NS}}}embed"):
                    raise _error("商品图片缺少有效图片引用", str(source_row))
                used.add(blips[0].get(f"{{{OFFICE_REL_NS}}}embed"))
            if used - {link.get("Id") for link in links}:
                raise _error("商品图片引用不存在")
            for link in list(links):
                if link.get("Id") not in used:
                    links.remove(link)
                    continue
                if not link.get("Type", "").endswith("/image"):
                    raise _error("图片关系类型无效")
                target = package.resolve_relationship_target(drawing_part, link.get("Target"))
                try:
                    _validate_media(package, target)
                except WorkbookError as error:
                    raise _error("商品图片损坏或格式不受支持", target) from error
                content = package.read(target)
                if not content.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
                    raise _error("仅支持 PNG / JPEG 商品图片", target)
                parts[target] = content
                image_hashes[target] = hashlib.sha256(content).hexdigest()
                link.set("Target", "/" + target)
            parts["xl/drawings/drawing1.xml"] = _xml(drawing)
            parts["xl/drawings/_rels/drawing1.xml.rels"] = _xml(links)
            drawing_node.set(f"{{{OFFICE_REL_NS}}}id", "drawing1")
            sheet_links = etree.Element(f"{{{PACKAGE_REL_NS}}}Relationships", nsmap={None: PACKAGE_REL_NS})
            etree.SubElement(sheet_links, f"{{{PACKAGE_REL_NS}}}Relationship", Id="drawing1", Type=REL+"drawing", Target="../drawings/drawing1.xml")
            parts["xl/worksheets/_rels/sheet1.xml.rels"] = _xml(sheet_links)
        if image_rows != set(row_map):
            raise _error("部分商品明细缺少图片", ",".join(str(r) for r in sorted(set(row_map)-image_rows)))
        parts["xl/worksheets/sheet1.xml"] = _xml(sheet)
        # Header strings and styles may refer to the source shared strings/theme.
        for name in package.names:
            if name in {"xl/sharedStrings.xml", "xl/theme/theme1.xml"}:
                parts[name] = package.read(name)
    references = [
        ("产品库", [[r.get(c, "") for c in "ABCD"] for r in catalog["product_rows"]]),
        ("FBA地址库", [["仓库编码", "目的地编码", "仓库类型", "联络人", "公司", "电话", "地址", "州省", "城市", "邮编"]] + [[w.get(c, "") for c in "ABCDEFGHIJ"] for w in catalog["warehouses"]]),
        ("base", [[r.get(c, "") for c in "ABCDEFGHI"] for r in catalog["base_rows"]]),
    ]
    workbook = etree.Element(f"{{{MAIN_NS}}}workbook", nsmap={None: MAIN_NS, "r": OFFICE_REL_NS})
    sheets = etree.SubElement(workbook, f"{{{MAIN_NS}}}sheets")
    rels = etree.Element(f"{{{PACKAGE_REL_NS}}}Relationships", nsmap={None: PACKAGE_REL_NS})
    for i, name in enumerate(["模版"] + [r[0] for r in references], 1):
        s = etree.SubElement(sheets, f"{{{MAIN_NS}}}sheet", name=name, sheetId=str(i))
        s.set(f"{{{OFFICE_REL_NS}}}id", f"sheet{i}")
        etree.SubElement(rels, f"{{{PACKAGE_REL_NS}}}Relationship", Id=f"sheet{i}", Type=REL+"worksheet", Target=f"worksheets/sheet{i}.xml")
        if i > 1:
            parts[f"xl/worksheets/sheet{i}.xml"] = _reference_sheet(references[i-2][1])
    for i, (target, kind) in enumerate((("styles.xml", "styles"), ("sharedStrings.xml", "sharedStrings"), ("theme/theme1.xml", "theme"))):
        if "xl/" + target in parts:
            etree.SubElement(rels, f"{{{PACKAGE_REL_NS}}}Relationship", Id=f"asset{i}", Type=REL+kind, Target=target)
    parts["xl/workbook.xml"] = _xml(workbook)
    parts["xl/_rels/workbook.xml.rels"] = _xml(rels)
    root_rels = etree.Element(f"{{{PACKAGE_REL_NS}}}Relationships", nsmap={None: PACKAGE_REL_NS})
    etree.SubElement(root_rels, f"{{{PACKAGE_REL_NS}}}Relationship", Id="book", Type=REL+"officeDocument", Target="xl/workbook.xml")
    parts["_rels/.rels"] = _xml(root_rels)
    content_types = etree.Element(f"{{{CT}}}Types", nsmap={None: CT})
    for ext, typ in (("rels", "application/vnd.openxmlformats-package.relationships+xml"), ("xml", "application/xml"), ("png", "image/png"), ("jpg", "image/jpeg"), ("jpeg", "image/jpeg")):
        etree.SubElement(content_types, f"{{{CT}}}Default", Extension=ext, ContentType=typ)
    kinds = {"xl/workbook.xml": "sheet.main", "xl/styles.xml": "styles", "xl/sharedStrings.xml": "sharedStrings"}
    for name in parts:
        if name.startswith("xl/worksheets/") and name.endswith(".xml"):
            kind = "worksheet"
        elif name == "xl/drawings/drawing1.xml":
            etree.SubElement(content_types, f"{{{CT}}}Override", PartName="/"+name, ContentType="application/vnd.openxmlformats-officedocument.drawing+xml")
            continue
        elif name == "xl/theme/theme1.xml":
            etree.SubElement(content_types, f"{{{CT}}}Override", PartName="/"+name, ContentType="application/vnd.openxmlformats-officedocument.theme+xml")
            continue
        elif name in kinds:
            kind = kinds[name]
        else:
            continue
        etree.SubElement(content_types, f"{{{CT}}}Override", PartName="/"+name, ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml."+kind+"+xml")
    parts["[Content_Types].xml"] = _xml(content_types)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination, suffix=".xlsx", delete=False) as stream:
            temporary = Path(stream.name)
        with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
            for name, payload in parts.items():
                archive.writestr(name, payload)
        with SafePackage.open(temporary) as checked:
            output_sheet = _sheet(checked, "模版")
            assert output_sheet is not None
            checked_bundle = parse_equator_package(checked, output_sheet[1])
            if len(checked_bundle.product_rows) != len(rows):
                raise _error("输出明细数量校验失败")
            for name, digest in image_hashes.items():
                if hashlib.sha256(checked.read(name)).hexdigest() != digest:
                    raise _error("输出图片校验失败", name)
        # Hard-link publication is atomic and never replaces an existing file.
        os.link(temporary, output)
        return output
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
