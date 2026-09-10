"""Create a tiny, anonymous OOXML workbook for XLSX engine tests."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from lxml import etree

from fis.xlsx.template import STANDARD_V1

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
VML_NS = "urn:schemas-microsoft-com:vml"
OFFICE_NS = "urn:schemas-microsoft-com:office:office"
EXCEL_NS = "urn:schemas-microsoft-com:office:excel"

_NSMAP = {None: MAIN_NS, "r": REL_NS}
_XDR_NSMAP = {None: XDR_NS, "xdr": XDR_NS, "a": DRAWING_NS, "r": REL_NS}
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02\x00\x00\x00\x0bIDATx\xda"
    b"cd\xf8\x0f\x00\x01\x05\x01\x01'\x18\xe3f\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _xml(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _element(parent: etree._Element, tag_name: str, **attributes: str) -> etree._Element:
    child = etree.SubElement(parent, f"{{{MAIN_NS}}}{tag_name}")
    child.attrib.update(attributes)
    return child


def _inline_cell(row: etree._Element, ref: str, value: str, *, style: str | None = None) -> None:
    attributes = {"r": ref, "t": "inlineStr"}
    if style is not None:
        attributes["s"] = style
    cell = _element(row, "c", **attributes)
    inline = _element(cell, "is")
    text = _element(inline, "t")
    text.text = value


def _value_cell(row: etree._Element, ref: str, value: str, *, style: str | None = None) -> None:
    attributes = {"r": ref}
    if style is not None:
        attributes["s"] = style
    cell = _element(row, "c", **attributes)
    value_element = _element(cell, "v")
    value_element.text = value


def _formula_cell(row: etree._Element, ref: str, formula: str, cached: str) -> None:
    cell = _element(row, "c", r=ref, s="0")
    _element(cell, "f").text = formula
    _element(cell, "v").text = cached


def _normalize_shipments(shipments: Mapping[str, Any] | Iterable[Any]) -> list[tuple[str, int]]:
    """Normalize convenient synthetic input into ``(shipment_id, item_count)``."""

    entries = shipments.items() if isinstance(shipments, Mapping) else shipments
    normalized: list[tuple[str, int]] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if isinstance(entry, tuple) and len(entry) == 2:
            shipment_id, count = entry
        else:
            shipment_id, count = entry, 1
        if not isinstance(shipment_id, str) or not shipment_id.strip():
            raise ValueError("shipment IDs must be non-empty strings")
        if shipment_id in seen_ids:
            raise ValueError("shipment IDs must be unique")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("shipment item counts must be positive integers")
        seen_ids.add(shipment_id)
        normalized.append((shipment_id, count))
    if not normalized:
        raise ValueError("at least one shipment is required")
    return normalized


def _content_types() -> bytes:
    root = etree.Element(f"{{{CT_NS}}}Types", nsmap={None: CT_NS})
    for extension, content_type in (
        ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
        ("xml", "application/xml"),
        ("png", "image/png"),
        ("vml", "application/vnd.openxmlformats-officedocument.vmlDrawing"),
    ):
        etree.SubElement(root, f"{{{CT_NS}}}Default", Extension=extension, ContentType=content_type)
    for part_name, content_type in (
        (
            "/xl/workbook.xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        ),
        (
            "/xl/worksheets/sheet1.xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
        ),
        (
            "/xl/styles.xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml",
        ),
        (
            "/xl/drawings/drawing1.xml",
            "application/vnd.openxmlformats-officedocument.drawing+xml",
        ),
        (
            "/xl/comments1.xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml",
        ),
    ):
        etree.SubElement(root, f"{{{CT_NS}}}Override", PartName=part_name, ContentType=content_type)
    return _xml(root)


def _root_relationships() -> bytes:
    root = etree.Element(f"{{{PKG_REL_NS}}}Relationships", nsmap={None: PKG_REL_NS})
    etree.SubElement(
        root,
        f"{{{PKG_REL_NS}}}Relationship",
        Id="rId1",
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
        Target="xl/workbook.xml",
    )
    return _xml(root)


def _workbook() -> bytes:
    root = etree.Element(f"{{{MAIN_NS}}}workbook", nsmap=_NSMAP)
    book_views = _element(root, "bookViews")
    _element(book_views, "workbookView", activeTab="0")
    sheets = _element(root, "sheets")
    sheet = _element(sheets, "sheet", name=STANDARD_V1.sheet_name, sheetId="1")
    sheet.set(f"{{{REL_NS}}}id", "rId1")
    _element(root, "calcPr", calcId="0")
    return _xml(root)


def _workbook_relationships() -> bytes:
    root = etree.Element(f"{{{PKG_REL_NS}}}Relationships", nsmap={None: PKG_REL_NS})
    for rel_id, rel_type, target in (
        (
            "rId1",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
            "worksheets/sheet1.xml",
        ),
        (
            "rId2",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
            "styles.xml",
        ),
    ):
        etree.SubElement(
            root, f"{{{PKG_REL_NS}}}Relationship", Id=rel_id, Type=rel_type, Target=target
        )
    return _xml(root)


def _styles() -> bytes:
    root = etree.Element(f"{{{MAIN_NS}}}styleSheet", nsmap={None: MAIN_NS})
    fonts = _element(root, "fonts", count="1")
    font = _element(fonts, "font")
    _element(font, "sz", val="11")
    _element(font, "name", val="Calibri")
    fills = _element(root, "fills", count="2")
    _element(_element(fills, "fill"), "patternFill", patternType="none")
    _element(_element(fills, "fill"), "patternFill", patternType="gray125")
    borders = _element(root, "borders", count="1")
    _element(borders, "border")
    cell_style_xfs = _element(root, "cellStyleXfs", count="1")
    _element(cell_style_xfs, "xf", numFmtId="0", fontId="0", fillId="0", borderId="0")
    cell_xfs = _element(root, "cellXfs", count="1")
    _element(cell_xfs, "xf", numFmtId="0", fontId="0", fillId="0", borderId="0", xfId="0")
    cell_styles = _element(root, "cellStyles", count="1")
    _element(cell_styles, "cellStyle", name="Normal", xfId="0", builtinId="0")
    return _xml(root)


def _worksheet(shipment_rows: list[tuple[int, str, int]], total_row: int) -> bytes:
    root = etree.Element(f"{{{MAIN_NS}}}worksheet", nsmap=_NSMAP)
    dimension = _element(root, "dimension", ref=f"A1:Z{total_row}")
    del dimension
    sheet_views = _element(root, "sheetViews")
    sheet_view = _element(sheet_views, "sheetView", workbookViewId="0")
    selected_reference = f"Q{total_row - 2}"
    _element(sheet_view, "pane", topLeftCell=selected_reference)
    _element(
        sheet_view,
        "selection",
        activeCell=selected_reference,
        sqref=selected_reference,
    )
    sheet_data = _element(root, "sheetData")

    for row_number in range(1, 11):
        row = _element(sheet_data, "row", r=str(row_number))
        if row_number == 4:
            _inline_cell(row, "C4", "SYNTHETIC-WAREHOUSE", style="0")
        elif row_number == 5:
            _inline_cell(row, "C5", "SYNTHETIC-ADDRESS", style="0")
        elif row_number == 6:
            _inline_cell(row, "C6", "SYNTHETIC-CHANNEL", style="0")
        elif row_number in {8, 9, 10}:
            _inline_cell(row, f"C{row_number}", "SYNTHETIC-VALUE", style="0")
            _value_cell(row, f"G{row_number}", "0", style="0")
            if row_number == 10:
                _formula_cell(row, "O10", f"P{total_row}/N{total_row}", "0")
                _formula_cell(row, "R10", "G9/G10", "0")

    header_row = _element(sheet_data, "row", r="11")
    for column in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        headers = {
            "A": "货件编号SHIPMENT ID\n(亚马逊货物必填）\n不可合并",
            "B": "跟踪编号\nREFERENCE ID\n(亚马逊货物必填）\n不可合并",
            "C": "图片\nPRODUCT PICTURE\n(必填)",
        }
        _inline_cell(header_row, f"{column}11", headers.get(column, f"Header {column}"))

    for row_number, shipment_id, item_number in shipment_rows:
        row = _element(sheet_data, "row", r=str(row_number))
        values = {
            "A": shipment_id,
            "B": f"Synthetic Product {item_number}",
            "C": f"Synthetic SKU {item_number}",
            "D": "1",
            "E": "Synthetic Unit",
            "F": "1",
            "G": "Synthetic Country",
            "H": "EA",
            "I": "1",
            "J": "0",
            "K": "1",
            "L": "0",
            "M": "1",
            "N": "0.001",
            "O": "USD",
            "P": "10",
        }
        for column, value in values.items():
            _inline_cell(row, f"{column}{row_number}", value)

    _element(sheet_data, "row", r=str(total_row - 1))
    total = _element(sheet_data, "row", r=str(total_row))
    _inline_cell(total, f"A{total_row}", "合计TOTAL")
    _value_cell(total, f"K{total_row}", str(len(shipment_rows)), style="0")
    _value_cell(total, f"M{total_row}", str(len(shipment_rows)), style="0")
    _value_cell(total, f"N{total_row}", f"{len(shipment_rows) * 0.001:g}", style="0")
    _value_cell(total, f"P{total_row}", str(len(shipment_rows) * 10), style="0")

    merge_cells = _element(root, "mergeCells", count=str(len(STANDARD_V1.value_ranges)))
    for cell_range in STANDARD_V1.value_ranges.values():
        _element(merge_cells, "mergeCell", ref=cell_range)

    validations = _element(root, "dataValidations", count="1")
    broken_list = _element(
        validations,
        "dataValidation",
        type="list",
        allowBlank="1",
        showInputMessage="1",
        showErrorMessage="1",
        sqref="O4:P4 R5:S5",
    )
    _element(broken_list, "formula1").text = "#REF!"

    drawing = _element(root, "drawing")
    drawing.set(f"{{{REL_NS}}}id", "rId1")
    legacy = _element(root, "legacyDrawing")
    legacy.set(f"{{{REL_NS}}}id", "rId3")
    return _xml(root)


def _comments() -> bytes:
    root = etree.Element(f"{{{MAIN_NS}}}comments", nsmap={None: MAIN_NS})
    authors = _element(root, "authors")
    _element(authors, "author").text = "Synthetic Fixture"
    comment_list = _element(root, "commentList")
    for ref, text in (
        ("L11", "Synthetic cartons"),
        ("N11", "Synthetic volume"),
        ("P11", "Synthetic amount"),
    ):
        comment = _element(comment_list, "comment", ref=ref, authorId="0")
        comment_text = _element(comment, "text")
        run = _element(comment_text, "r")
        _element(run, "rPr")
        _element(run, "t").text = text
    return _xml(root)


def _worksheet_relationships() -> bytes:
    root = etree.Element(f"{{{PKG_REL_NS}}}Relationships", nsmap={None: PKG_REL_NS})
    relationships = (
        (
            "rId1",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing",
            "../drawings/drawing1.xml",
        ),
        (
            "rId2",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
            "../comments1.xml",
        ),
        (
            "rId3",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/vmlDrawing",
            "../drawings/vmlDrawing1.vml",
        ),
    )
    for rel_id, rel_type, target in relationships:
        etree.SubElement(
            root, f"{{{PKG_REL_NS}}}Relationship", Id=rel_id, Type=rel_type, Target=target
        )
    return _xml(root)


def _drawing(image_rows: list[int]) -> bytes:
    root = etree.Element(f"{{{XDR_NS}}}wsDr", nsmap=_XDR_NSMAP)
    for image_number, row_number in enumerate(image_rows, start=1):
        anchor = etree.SubElement(root, f"{{{XDR_NS}}}twoCellAnchor", editAs="oneCell")
        from_cell = etree.SubElement(anchor, f"{{{XDR_NS}}}from")
        etree.SubElement(from_cell, f"{{{XDR_NS}}}col").text = "2"
        etree.SubElement(from_cell, f"{{{XDR_NS}}}colOff").text = "0"
        etree.SubElement(from_cell, f"{{{XDR_NS}}}row").text = str(row_number - 1)
        etree.SubElement(from_cell, f"{{{XDR_NS}}}rowOff").text = "0"
        to_cell = etree.SubElement(anchor, f"{{{XDR_NS}}}to")
        etree.SubElement(to_cell, f"{{{XDR_NS}}}col").text = "3"
        etree.SubElement(to_cell, f"{{{XDR_NS}}}colOff").text = "0"
        etree.SubElement(to_cell, f"{{{XDR_NS}}}row").text = str(row_number)
        etree.SubElement(to_cell, f"{{{XDR_NS}}}rowOff").text = "0"

        picture = etree.SubElement(anchor, f"{{{XDR_NS}}}pic")
        nv = etree.SubElement(picture, f"{{{XDR_NS}}}nvPicPr")
        etree.SubElement(
            nv, f"{{{XDR_NS}}}cNvPr", id=str(image_number), name=f"Synthetic image {image_number}"
        )
        etree.SubElement(nv, f"{{{XDR_NS}}}cNvPicPr", preferRelativeResize="0")
        fill = etree.SubElement(picture, f"{{{XDR_NS}}}blipFill")
        blip = etree.SubElement(fill, f"{{{DRAWING_NS}}}blip")
        blip.set(f"{{{REL_NS}}}embed", f"rId{image_number}")
        stretch = etree.SubElement(fill, f"{{{DRAWING_NS}}}stretch")
        etree.SubElement(stretch, f"{{{DRAWING_NS}}}fillRect")
        shape = etree.SubElement(picture, f"{{{XDR_NS}}}spPr")
        geometry = etree.SubElement(shape, f"{{{DRAWING_NS}}}prstGeom", prst="rect")
        etree.SubElement(geometry, f"{{{DRAWING_NS}}}avLst")
        etree.SubElement(anchor, f"{{{XDR_NS}}}clientData")
    return _xml(root)


def _drawing_relationships(image_rows: list[int]) -> bytes:
    root = etree.Element(f"{{{PKG_REL_NS}}}Relationships", nsmap={None: PKG_REL_NS})
    for image_number in range(1, len(image_rows) + 1):
        etree.SubElement(
            root,
            f"{{{PKG_REL_NS}}}Relationship",
            Id=f"rId{image_number}",
            Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
            Target=f"../media/image{image_number}.png",
        )
    return _xml(root)


def _vml() -> bytes:
    nsmap = {"v": VML_NS, "o": OFFICE_NS, "x": EXCEL_NS}
    root = etree.Element("xml", nsmap=nsmap)
    layout = etree.SubElement(root, f"{{{OFFICE_NS}}}shapelayout")
    layout.set(f"{{{VML_NS}}}ext", "edit")
    idmap = etree.SubElement(layout, f"{{{OFFICE_NS}}}idmap", data="1")
    idmap.set(f"{{{VML_NS}}}ext", "edit")
    shape_type = etree.SubElement(
        root,
        f"{{{VML_NS}}}shapetype",
        id="_x0000_t202",
        coordsize="21600,21600",
        path="m,l,21600r21600,l21600,xe",
    )
    shape_type.set(f"{{{VML_NS}}}ext", "edit")
    shape_type.set(f"{{{OFFICE_NS}}}spt", "202")
    etree.SubElement(shape_type, f"{{{VML_NS}}}stroke", joinstyle="miter")
    path = etree.SubElement(shape_type, f"{{{VML_NS}}}path", gradientshapeok="t")
    path.set(f"{{{OFFICE_NS}}}connecttype", "rect")
    for index, column in enumerate((11, 13, 15), start=1025):
        shape = etree.SubElement(
            root,
            f"{{{VML_NS}}}shape",
            id=f"_x0000_s{index}",
            type="#_x0000_t202",
            style="position:absolute; margin-left:59.25pt; margin-top:1.5pt; width:108pt; height:59.25pt; z-index:1; visibility:hidden",
            fillcolor="#ffffe1",
            strokecolor="#000000",
        )
        shape.set(f"{{{OFFICE_NS}}}insetmode", "auto")
        client = etree.SubElement(shape, f"{{{EXCEL_NS}}}ClientData", ObjectType="Note")
        etree.SubElement(client, f"{{{EXCEL_NS}}}MoveWithCells")
        etree.SubElement(client, f"{{{EXCEL_NS}}}SizeWithCells")
        etree.SubElement(client, f"{{{EXCEL_NS}}}Row").text = "10"
        etree.SubElement(client, f"{{{EXCEL_NS}}}Column").text = str(column)
    return _xml(root)


def build_standard_v1(
    path: Path, shipments: Mapping[str, Any] | Iterable[Any], image_rows: Iterable[int]
) -> None:
    """Build a deterministic, synthetic standard-v1 workbook.

    ``shipments`` is normally a mapping from shipment ID to positive item
    count.  An iterable of IDs is also accepted for concise fixture setup.
    ``image_rows`` contains one-based worksheet rows for synthetic product
    images.
    """

    normalized = _normalize_shipments(shipments)
    image_row_list = list(image_rows)

    shipment_rows: list[tuple[int, str, int]] = []
    row_number = STANDARD_V1.product_start_row
    item_number = 1
    for shipment_id, count in normalized:
        for _ in range(count):
            shipment_rows.append((row_number, shipment_id, item_number))
            row_number += 1
            item_number += 1
    separator_row = row_number
    total_row = separator_row + 1
    product_rows = set(range(STANDARD_V1.product_start_row, separator_row))
    if len(image_row_list) != len(set(image_row_list)) or any(
        isinstance(row, bool) or not isinstance(row, int) or row not in product_rows
        for row in image_row_list
    ):
        raise ValueError("image rows must be unique rows in the product region")

    path.parent.mkdir(parents=True, exist_ok=True)
    parts: dict[str, bytes] = {
        "[Content_Types].xml": _content_types(),
        "_rels/.rels": _root_relationships(),
        "xl/workbook.xml": _workbook(),
        "xl/_rels/workbook.xml.rels": _workbook_relationships(),
        "xl/styles.xml": _styles(),
        "xl/worksheets/sheet1.xml": _worksheet(shipment_rows, total_row),
        "xl/worksheets/_rels/sheet1.xml.rels": _worksheet_relationships(),
        "xl/comments1.xml": _comments(),
        "xl/drawings/drawing1.xml": _drawing(image_row_list),
        "xl/drawings/_rels/drawing1.xml.rels": _drawing_relationships(image_row_list),
        "xl/drawings/vmlDrawing1.vml": _vml(),
    }
    for image_number in range(1, len(image_row_list) + 1):
        parts[f"xl/media/image{image_number}.png"] = _PNG_1X1

    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for part_name, payload in parts.items():
            info = ZipInfo(part_name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload)
