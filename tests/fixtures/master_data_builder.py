"""Build a synthetic workbook shaped like the real master-data template."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

FBA_SHEET = "FBA仓代码&详细地址"
CHANNEL_SHEET = "渠道"
DELIVERY_SHEET = "国内交货仓"


def _column(index: int) -> str:
    name = ""
    while index >= 0:
        name = chr(ord("A") + index % 26) + name
        index = index // 26 - 1
    return name


def _sheet_xml(rows: Sequence[Sequence[str]]) -> bytes:
    body = []
    for number, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{_column(position)}{number}" t="inlineStr">'
            f"<is><t>{escape(value)}</t></is></c>"
            for position, value in enumerate(row)
            if value != ""
        )
        body.append(f'<row r="{number}">{cells}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{MAIN_NS}"><sheetData>{"".join(body)}</sheetData></worksheet>'
    ).encode()


def build_master_data(
    path: Path,
    *,
    sheets: Mapping[str, Sequence[Sequence[str]]] | None = None,
) -> Path:
    """Write a master-data workbook. ``sheets`` maps a sheet name to its rows."""

    content = dict(sheets) if sheets is not None else {
        FBA_SHEET: [["FBA仓代码", "FBA仓详细地址"]],
        CHANNEL_SHEET: [["渠道"]],
        DELIVERY_SHEET: [["国内交货仓"]],
    }

    names = list(content)
    sheet_tags = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(names, start=1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{MAIN_NS}" xmlns:r="{OFFICE_REL_NS}">'
        f"<sheets>{sheet_tags}</sheets></workbook>"
    ).encode()

    relationships = "".join(
        f'<Relationship Id="rId{index}" Target="worksheets/sheet{index}.xml" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
        for index in range(1, len(names) + 1)
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PACKAGE_REL_NS}">{relationships}</Relationships>'
    ).encode()

    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, len(names) + 1)
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{CONTENT_TYPES_NS}">'
        '<Default Extension="rels" ContentType='
        '"application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f"{overrides}</Types>"
    ).encode()

    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PACKAGE_REL_NS}">'
        '<Relationship Id="rIdWorkbook" Target="xl/workbook.xml" Type='
        '"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
        "</Relationships>"
    ).encode()

    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        for index, name in enumerate(names, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(content[name]))
    return path
