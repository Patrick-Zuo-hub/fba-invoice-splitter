from __future__ import annotations

import posixpath
import struct
import time
import warnings
from pathlib import Path
from zipfile import ZipFile

import pytest
from lxml import etree

from fis.xlsx.template import STANDARD_V1
from tests.fixtures.standard_v1_builder import build_standard_v1

NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
VML_NS = "urn:schemas-microsoft-com:vml"
OFFICE_NS = "urn:schemas-microsoft-com:office:office"
EXCEL_NS = "urn:schemas-microsoft-com:office:excel"


def test_standard_v1_contract_is_deeply_immutable() -> None:
    assert STANDARD_V1.type == "fba-customs-standard"
    assert STANDARD_V1.version == "v1"
    assert STANDARD_V1.sheet_name == "装箱单&发票 发货信息"
    assert STANDARD_V1.product_start_row == 12
    assert STANDARD_V1.value_ranges == {
        "warehouse_code": "C4:F4",
        "warehouse_address": "C5:F5",
        "shipping_channel": "C6:F6",
        "delivery_date": "C8:D8",
        "sailing_date": "C9:D9",
        "delivery_warehouse": "C10:D10",
    }
    assert STANDARD_V1.total_columns == {
        "cartons": "K",
        "gross_weight": "M",
        "volume": "N",
        "amount": "P",
    }

    with pytest.raises(TypeError):
        STANDARD_V1.value_ranges["warehouse_code"] = "A1"  # type: ignore[index]
    with pytest.raises(TypeError):
        STANDARD_V1.total_columns["amount"] = "Z"  # type: ignore[index]


def test_standard_v1_builder_emits_valid_anonymous_fixture(tmp_path: Path) -> None:
    output = tmp_path / "standard-v1.xlsx"
    build_standard_v1(output, {"SYNTHETIC-1": 1, "SYNTHETIC-2": 1}, [12, 13])

    with ZipFile(output) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        assert "xl/workbook.xml" in names
        assert "xl/worksheets/sheet1.xml" in names
        assert "xl/comments1.xml" in names
        assert "xl/drawings/drawing1.xml" in names
        assert "xl/media/image1.png" in names
        assert "xl/media/image2.png" in names

        workbook = etree.fromstring(archive.read("xl/workbook.xml"))
        sheet = workbook.xpath("/main:workbook/main:sheets/main:sheet", namespaces=NS)[0]
        assert sheet.get("name") == "装箱单&发票 发货信息"

        worksheet = etree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        merge_refs = worksheet.xpath(
            "/main:worksheet/main:mergeCells/main:mergeCell/@ref", namespaces=NS
        )
        assert merge_refs == list(STANDARD_V1.value_ranges.values())
        header_refs = worksheet.xpath(
            "/main:worksheet/main:sheetData/main:row[@r='11']/main:c/@r", namespaces=NS
        )
        assert header_refs == [f"{column}11" for column in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
        assert worksheet.xpath(
            "/main:worksheet/main:sheetData/main:row[@r='12']/main:c[@r='A12']", namespaces=NS
        )
        assert worksheet.xpath(
            "/main:worksheet/main:sheetData/main:row[@r='13']/main:c[@r='A13']", namespaces=NS
        )
        assert worksheet.xpath(
            "/main:worksheet/main:sheetData/main:row[@r='14'][not(main:c)]", namespaces=NS
        )
        assert (
            worksheet.xpath(
                "string(/main:worksheet/main:sheetData/main:row[@r='15']/main:c[@r='A15']/main:is/main:t)",
                namespaces=NS,
            )
            == "合计TOTAL"
        )

        comments = etree.fromstring(archive.read("xl/comments1.xml"))
        assert comments.xpath(
            "/main:comments/main:commentList/main:comment/@ref", namespaces=NS
        ) == ["L11", "N11", "P11"]

        drawing = etree.fromstring(archive.read("xl/drawings/drawing1.xml"))
        anchors = drawing.xpath("/xdr:wsDr/xdr:twoCellAnchor", namespaces=NS)
        assert len(anchors) == 2
        assert [
            (
                int(anchor.xpath("string(xdr:from/xdr:col)", namespaces=NS)),
                int(anchor.xpath("string(xdr:from/xdr:row)", namespaces=NS)),
                int(anchor.xpath("string(xdr:to/xdr:col)", namespaces=NS)),
                int(anchor.xpath("string(xdr:to/xdr:row)", namespaces=NS)),
            )
            for anchor in anchors
        ] == [(2, 11, 3, 12), (2, 12, 3, 13)]

        drawing_relationships = etree.fromstring(
            archive.read("xl/drawings/_rels/drawing1.xml.rels")
        )
        assert [relationship.get("Target") for relationship in drawing_relationships] == [
            "../media/image1.png",
            "../media/image2.png",
        ]

        content_types = etree.fromstring(archive.read("[Content_Types].xml"))
        overrides = {
            override.get("PartName"): override.get("ContentType")
            for override in content_types
            if override.tag.endswith("Override")
        }
        assert overrides["/xl/drawings/drawing1.xml"].endswith("drawing+xml")
        assert overrides["/xl/comments1.xml"].endswith("comments+xml")

        worksheet_relationships = etree.fromstring(
            archive.read("xl/worksheets/_rels/sheet1.xml.rels")
        )
        worksheet_targets = {relationship.get("Target") for relationship in worksheet_relationships}
        assert "../drawings/drawing1.xml" in worksheet_targets
        assert "../comments1.xml" in worksheet_targets
        assert "../drawings/vmlDrawing1.vml" in worksheet_targets

        image1 = archive.read("xl/media/image1.png")
        image2 = archive.read("xl/media/image2.png")
        assert image1 == image2
        assert image1.startswith(b"\x89PNG\r\n\x1a\n")
        assert b"ONT8" not in archive.read("xl/worksheets/sheet1.xml")
        assert "客户".encode() not in b"".join(archive.read(name) for name in names)


def test_standard_v1_vml_comments_use_excel_namespaces_and_coordinates(tmp_path: Path) -> None:
    output = tmp_path / "standard-v1.xlsx"
    build_standard_v1(output, {"SYNTHETIC-1": 1}, [12])

    with ZipFile(output) as archive:
        vml = etree.fromstring(archive.read("xl/drawings/vmlDrawing1.vml"))
        assert etree.QName(vml).localname == "xml"
        assert vml.nsmap["v"] == VML_NS
        assert vml.nsmap["o"] == OFFICE_NS
        assert vml.nsmap["x"] == EXCEL_NS

        shape_type = vml.find(f"{{{VML_NS}}}shapetype")
        assert shape_type is not None
        assert shape_type.get(f"{{{OFFICE_NS}}}spt") == "202"
        assert shape_type.get("id") == "_x0000_t202"
        assert shape_type.get(f"{{{OFFICE_NS}}}connecttype") is None
        assert shape_type.find(f"{{{VML_NS}}}path").get(f"{{{OFFICE_NS}}}connecttype") == "rect"

        shapes = vml.findall(f"{{{VML_NS}}}shape")
        assert len(shapes) == 3
        assert [shape.get(f"{{{OFFICE_NS}}}insetmode") for shape in shapes] == [
            "auto",
            "auto",
            "auto",
        ]
        assert all(shape.get("type") == "#_x0000_t202" for shape in shapes)
        assert [
            (
                int(shape.find(f"{{{EXCEL_NS}}}ClientData/{{{EXCEL_NS}}}Row").text),
                int(shape.find(f"{{{EXCEL_NS}}}ClientData/{{{EXCEL_NS}}}Column").text),
            )
            for shape in shapes
        ] == [(10, 11), (10, 13), (10, 15)]

        comments = etree.fromstring(archive.read("xl/comments1.xml"))
        comment_refs = comments.xpath(
            "/main:comments/main:commentList/main:comment/@ref", namespaces=NS
        )
        assert comment_refs == ["L11", "N11", "P11"]
        worksheet = etree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        assert (
            worksheet.xpath("string(/main:worksheet/main:legacyDrawing/@r:id)", namespaces=NS)
            == "rId3"
        )
        worksheet_rels = etree.fromstring(archive.read("xl/worksheets/_rels/sheet1.xml.rels"))
        relation_by_type = {
            relation.get("Type"): relation.get("Target") for relation in worksheet_rels
        }
        assert (
            relation_by_type[
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
            ]
            == "../comments1.xml"
        )
        assert (
            relation_by_type[
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/vmlDrawing"
            ]
            == "../drawings/vmlDrawing1.vml"
        )


def test_standard_v1_builder_is_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.xlsx"
    second = tmp_path / "second.xlsx"
    shipments = {"SYNTHETIC-1": 1, "SYNTHETIC-2": 1}
    build_standard_v1(first, shipments, [12, 13])
    time.sleep(2)
    build_standard_v1(second, shipments, [12, 13])

    assert first.read_bytes() == second.read_bytes()


def test_standard_v1_styles_declare_default_normal_style(tmp_path: Path) -> None:
    output = tmp_path / "standard-v1.xlsx"
    build_standard_v1(output, {"SYNTHETIC-1": 1}, [])

    with ZipFile(output) as archive:
        styles = etree.fromstring(archive.read("xl/styles.xml"))
        cell_styles = styles.xpath("/main:styleSheet/main:cellStyles/main:cellStyle", namespaces=NS)
        assert [
            (style.get("name"), style.get("xfId"), style.get("builtinId")) for style in cell_styles
        ] == [("Normal", "0", "0")]

        try:
            from openpyxl import load_workbook
        except ImportError:
            return
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            load_workbook(output)
        assert not any("no default style" in str(item.message).lower() for item in captured)


@pytest.mark.parametrize("image_rows", [[14], [15], [99], [12, 12]])
def test_standard_v1_rejects_image_rows_outside_or_repeated_product_rows(
    tmp_path: Path, image_rows: list[int]
) -> None:
    with pytest.raises(ValueError, match="image row"):
        build_standard_v1(
            tmp_path / "invalid.xlsx", {"SYNTHETIC-1": 1, "SYNTHETIC-2": 1}, image_rows
        )


@pytest.mark.parametrize(
    "shipments",
    [
        {},
        {"": 1},
        {"SYNTHETIC-1": 0},
        {"SYNTHETIC-1": -1},
        {"SYNTHETIC-1": True},
        {"SYNTHETIC-1": 1.0},
    ],
)
def test_standard_v1_rejects_invalid_shipments(
    tmp_path: Path, shipments: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        build_standard_v1(tmp_path / "invalid.xlsx", shipments, [])


def test_standard_v1_workbook_view_uses_valid_ooxml_attributes(tmp_path: Path) -> None:
    output = tmp_path / "standard-v1.xlsx"
    build_standard_v1(output, {"SYNTHETIC-1": 1}, [])

    with ZipFile(output) as archive:
        workbook = etree.fromstring(archive.read("xl/workbook.xml"))
        views = workbook.xpath("/main:workbook/main:bookViews/main:workbookView", namespaces=NS)
        assert len(views) == 1
        assert "workbookViewId" not in views[0].attrib


def test_standard_v1_images_have_complete_relationship_and_png_contract(tmp_path: Path) -> None:
    output = tmp_path / "standard-v1.xlsx"
    build_standard_v1(output, {"SYNTHETIC-1": 1, "SYNTHETIC-2": 1}, [12, 13])

    with ZipFile(output) as archive:
        names = set(archive.namelist())
        drawing = etree.fromstring(archive.read("xl/drawings/drawing1.xml"))
        anchors = drawing.xpath("/xdr:wsDr/xdr:twoCellAnchor", namespaces=NS)
        drawing_relationships = etree.fromstring(
            archive.read("xl/drawings/_rels/drawing1.xml.rels")
        )
        relationships = {
            relationship.get("Id"): relationship.get("Target")
            for relationship in drawing_relationships
        }
        assert len(relationships) == len(list(drawing_relationships)) == len(anchors)
        assert len(set(relationships)) == len(relationships)

        embeds = [
            anchor.xpath("string(xdr:pic/xdr:blipFill/a:blip/@r:embed)", namespaces=NS)
            for anchor in anchors
        ]
        assert len(embeds) == len(set(embeds)) == len(anchors)
        assert set(embeds) == set(relationships)

        non_visual_ids = [
            element.get("id")
            for element in drawing.xpath(
                "/xdr:wsDr/xdr:twoCellAnchor/xdr:pic/xdr:nvPicPr/xdr:cNvPr", namespaces=NS
            )
        ]
        assert len(non_visual_ids) == len(set(non_visual_ids)) == len(anchors)

        for anchor, expected_row in zip(anchors, (12, 13), strict=True):
            assert anchor.get("editAs") == "oneCell"
            assert int(anchor.xpath("string(xdr:from/xdr:col)", namespaces=NS)) == 2
            assert int(anchor.xpath("string(xdr:to/xdr:col)", namespaces=NS)) == 3
            assert int(anchor.xpath("string(xdr:from/xdr:row)", namespaces=NS)) == expected_row - 1
            assert int(anchor.xpath("string(xdr:to/xdr:row)", namespaces=NS)) == expected_row

        for relationship_id, target in relationships.items():
            assert relationship_id in embeds
            assert target is not None
            media_name = posixpath.normpath(posixpath.join("xl/drawings", target))
            assert media_name in names
            png = archive.read(media_name)
            assert png.startswith(b"\x89PNG\r\n\x1a\n")
            assert png[12:16] == b"IHDR"
            width, height = struct.unpack(">II", png[16:24])
            assert (width, height) == (1, 1)

        content_types = etree.fromstring(archive.read("[Content_Types].xml"))
        png_defaults = [
            default
            for default in content_types
            if default.tag.endswith("Default") and default.get("Extension") == "png"
        ]
        png_overrides = [
            override
            for override in content_types
            if override.tag.endswith("Override")
            and (override.get("PartName") or "").startswith("/xl/media/image")
        ]
        assert png_defaults or len(png_overrides) == len(anchors)
        if png_defaults:
            assert png_defaults[0].get("ContentType") == "image/png"
        if png_overrides:
            assert {override.get("ContentType") for override in png_overrides} == {"image/png"}
