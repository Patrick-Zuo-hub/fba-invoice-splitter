"""Synthetic Equator invoice with two FBA shipments and four real image anchors."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree

from fis.xlsx.equator import CT, REL, XDR, A, _cell, _xml, default_catalog
from fis.xlsx.parser import MAIN_NS, OFFICE_REL_NS, PACKAGE_REL_NS
from tests.fixtures.standard_v1_builder import _PNG_1X1, _styles

IDS = ("FBA000000001", "FBA000000002")


def build_equator(path: Path, *, external: bool = False) -> None:
    sheet = etree.Element(f"{{{MAIN_NS}}}worksheet", nsmap={None: MAIN_NS, "r": OFFICE_REL_NS})
    data = etree.SubElement(sheet, f"{{{MAIN_NS}}}sheetData")
    head = etree.SubElement(data, f"{{{MAIN_NS}}}row", r="4")
    for col, value in default_catalog()["headers"].items():
        _cell(head, col, value)
    drawing = etree.Element(f"{{{XDR}}}wsDr", nsmap={"xdr": XDR, "a": A, "r": OFFICE_REL_NS})
    for index, cartons in enumerate((2, 8, 2, 7)):
        number = index + 5
        row = etree.SubElement(data, f"{{{MAIN_NS}}}row", r=str(number), ht="60", customHeight="1")
        fields = {"U": IDS[index // 2], "V": "ABCDEFGH", "W": "否", "X": str(cartons), "Y": "12.01", "Z": "46.99", "AA": "54.01", "AB": "51.01", "AC": "organizer", "AD": "收纳盒", "AE": "Plastic", "AF": "塑料", "AJ": "20", "AK": str(cartons*20), "AL": "15", "AM": str(cartons*300), "AN": "USD", "AO": "7323991000", "AP": "否", "AQ": "否", "B": "ONE-SOURCE-ORDER"}
        for col in default_catalog()["headers"]:
            _cell(row, col, fields.get(col, ""))
        anchor = etree.SubElement(drawing, f"{{{XDR}}}twoCellAnchor")
        for tag, n in (("from", number-1), ("to", number)):
            marker = etree.SubElement(anchor, f"{{{XDR}}}{tag}")
            for key, value in (("col", 19), ("colOff", 0), ("row", n), ("rowOff", 0)):
                etree.SubElement(marker, f"{{{XDR}}}{key}").text = str(value)
        pic = etree.SubElement(anchor, f"{{{XDR}}}pic")
        blip = etree.SubElement(etree.SubElement(pic, f"{{{XDR}}}blipFill"), f"{{{A}}}blip")
        blip.set(f"{{{OFFICE_REL_NS}}}embed", "img")
        etree.SubElement(anchor, f"{{{XDR}}}clientData")
    node = etree.SubElement(sheet, f"{{{MAIN_NS}}}drawing")
    node.set(f"{{{OFFICE_REL_NS}}}id", "drawing")
    book = etree.Element(f"{{{MAIN_NS}}}workbook", nsmap={None: MAIN_NS, "r": OFFICE_REL_NS})
    item = etree.SubElement(etree.SubElement(book, f"{{{MAIN_NS}}}sheets"), f"{{{MAIN_NS}}}sheet", name="模版", sheetId="1")
    item.set(f"{{{OFFICE_REL_NS}}}id", "sheet")

    def links(entries: list[tuple[str, str, str, str]]) -> bytes:
        root = etree.Element(f"{{{PACKAGE_REL_NS}}}Relationships", nsmap={None: PACKAGE_REL_NS})
        for id_, kind, target, mode in entries:
            rel = etree.SubElement(root, f"{{{PACKAGE_REL_NS}}}Relationship", Id=id_, Type=REL+kind, Target=target)
            if mode:
                rel.set("TargetMode", mode)
        return _xml(root)

    relations = [("sheet", "worksheet", "worksheets/sheet1.xml", ""), ("styles", "styles", "styles.xml", "")]
    parts = {
        "xl/workbook.xml": _xml(book), "xl/styles.xml": _styles(),
        "xl/worksheets/sheet1.xml": _xml(sheet),
        "_rels/.rels": links([("book", "officeDocument", "xl/workbook.xml", "")]),
        "xl/worksheets/_rels/sheet1.xml.rels": links([("drawing", "drawing", "../drawings/drawing1.xml", "")]),
        "xl/drawings/drawing1.xml": _xml(drawing),
        "xl/drawings/_rels/drawing1.xml.rels": links([("img", "image", "../media/test.png", "")]),
        "xl/media/test.png": _PNG_1X1,
    }
    if external:
        relations.append(("external", "externalLink", "externalLinks/externalLink1.xml", ""))
        parts["xl/externalLinks/externalLink1.xml"] = b"<externalLink/>"
        parts["xl/externalLinks/_rels/externalLink1.xml.rels"] = links([("source", "externalLinkPath", "https://example.invalid/lookup.xlsx", "External")])
    parts["xl/_rels/workbook.xml.rels"] = links(relations)
    types = etree.Element(f"{{{CT}}}Types", nsmap={None: CT})
    for extension, typ in (("xml", "application/xml"), ("rels", "application/vnd.openxmlformats-package.relationships+xml"), ("png", "image/png")):
        etree.SubElement(types, f"{{{CT}}}Default", Extension=extension, ContentType=typ)
    for name, typ in (("xl/workbook.xml", "sheet.main"), ("xl/styles.xml", "styles"), ("xl/worksheets/sheet1.xml", "worksheet")):
        etree.SubElement(types, f"{{{CT}}}Override", PartName="/"+name, ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml."+typ+"+xml")
    parts["[Content_Types].xml"] = _xml(types)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
