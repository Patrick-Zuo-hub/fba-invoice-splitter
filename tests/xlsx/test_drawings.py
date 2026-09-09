"""Supported OOXML drawing contracts for the FIS V1 worksheet."""

from __future__ import annotations

import base64
import io
import re
import struct
import zlib
from collections.abc import Callable
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image

from fis.xlsx.drawings import Anchor, classify_anchor, parse_workbook_drawings
from fis.xlsx.errors import WorkbookError
from fis.xlsx.package import SafePackage
from fis.xlsx.parser import ParsedBundle, parse_invoice_bundle
from tests.fixtures.standard_v1_builder import build_standard_v1

_JPEG_1X1 = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAGf/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9k="
)


def _pillow_image_bytes(format_name: str) -> bytes:
    with io.BytesIO() as stream:
        image_mode = "RGBA" if format_name == "PNG" else "RGB"
        color = (1, 2, 3, 255) if image_mode == "RGBA" else (1, 2, 3)
        Image.new(image_mode, (1, 1), color).save(stream, format=format_name)
        return stream.getvalue()


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def _rewrite_part(path: Path, part_name: str, rewrite: Callable[[bytes], bytes]) -> None:
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts[part_name] = rewrite(parts[part_name])
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


def _remove_drawing(path: Path) -> None:
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(
        b'<drawing r:id="rId1"/>',
        b"",
    )
    for name in tuple(parts):
        if name.startswith(("xl/drawings/drawing1", "xl/drawings/_rels/drawing1", "xl/media/")):
            parts.pop(name)
    parts["xl/worksheets/_rels/sheet1.xml.rels"] = parts[
        "xl/worksheets/_rels/sheet1.xml.rels"
    ].replace(
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/>',
        b"",
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


def _drawing_error(path: Path, code: str) -> WorkbookError:
    with pytest.raises(WorkbookError) as captured:
        parse_workbook_drawings(path, ParsedBundle((), (12,), 13, 14))
    assert captured.value.code == code
    return captured.value


def test_classifies_a_supported_product_anchor_to_its_one_based_source_row() -> None:
    anchor = Anchor(
        from_row=11,
        from_col=2,
        from_row_off=0,
        from_col_off=0,
        to_row=12,
        to_col=3,
        to_row_off=0,
        to_col_off=0,
        relationship_id="rId1",
    )

    assert classify_anchor(anchor, (12, 13)) == 12


@pytest.mark.parametrize(
    "anchor",
    [
        Anchor(11, 2, 10, 0, 11, 2, 9, 1, "row-offset-reverse"),
        Anchor(11, 2, 0, 10, 12, 2, 0, 9, "col-offset-reverse"),
        Anchor(11, 2, 0, 0, 11, 1, 0, 0, "left-of-start"),
    ],
)
def test_rejects_anchor_when_either_axis_ends_before_start(anchor: Anchor) -> None:
    with pytest.raises(WorkbookError) as captured:
        classify_anchor(anchor, (12, 13))
    assert captured.value.code == "INVALID_DRAWING"


@pytest.mark.parametrize(
    "anchor",
    [
        Anchor(11, 1, 0, 0, 12, 3, 0, 0, "rId1"),
        Anchor(11, 2, 1, 0, 12, 3, 0, 0, "rId1"),
        Anchor(11, 2, 0, 1, 12, 3, 0, 0, "rId1"),
        Anchor(11, 2, 0, 0, 12, 3, 1, 0, "rId1"),
        Anchor(11, 2, 0, 0, 13, 3, 0, 0, "rId1"),
    ],
)
def test_rejects_product_images_with_wrong_column_or_ambiguous_span(anchor: Anchor) -> None:
    with pytest.raises(WorkbookError) as captured:
        classify_anchor(anchor, (12, 13))
    assert captured.value.code == "AMBIGUOUS_IMAGE"


@pytest.mark.parametrize(
    "rewrite",
    [
        lambda xml: xml.replace(b"<clientData/>", b"<clientData><grpSp/></clientData>", 1),
        lambda xml: xml.replace(b"<col>2</col>", b'<col foo="1">2</col>', 1),
        lambda xml: xml.replace(b"<clientData/>", b"<clientData>text</clientData>", 1),
    ],
)
def test_rejects_invalid_anchor_subtrees(tmp_path: Path, rewrite: Callable[[bytes], bytes]) -> None:
    path = tmp_path / "invalid-anchor-subtree.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(path, "xl/drawings/drawing1.xml", rewrite)

    assert _drawing_error(path, "INVALID_DRAWING")


def test_preserves_a_header_logo_as_fixed_but_rejects_fixed_image_crossing_products() -> None:
    header_logo = Anchor(9, 0, 0, 0, 11, 1, 0, 0, "rId1")
    crossing = Anchor(9, 0, 0, 0, 12, 1, 1, 0, "rId2")

    assert classify_anchor(header_logo, (12, 13)) is None
    with pytest.raises(WorkbookError) as captured:
        classify_anchor(crossing, (12, 13))
    assert captured.value.code == "AMBIGUOUS_IMAGE"


def test_parses_two_product_images_and_keeps_inventory_deeply_immutable(tmp_path: Path) -> None:
    path = tmp_path / "standard.xlsx"
    build_standard_v1(path, [("S1", 1), ("S2", 1)], [12, 13])

    inventory = parse_workbook_drawings(path, parse_invoice_bundle(path))

    assert [image.source_row for image in inventory.product_images] == [12, 13]
    assert [image.relationship_id for image in inventory.product_images] == ["rId1", "rId2"]
    assert set(inventory.relationships) == {"rId1", "rId2"}
    with pytest.raises(TypeError):
        inventory.relationships["rId3"] = "xl/media/nope.png"  # type: ignore[index]


def test_keeps_a_header_logo_as_a_fixed_image_while_ignoring_comment_vml(tmp_path: Path) -> None:
    path = tmp_path / "fixed-logo.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(
        path,
        "xl/drawings/drawing1.xml",
        lambda xml: xml.replace(
            b"<from><col>2</col><colOff>0</colOff><row>11</row>",
            b"<from><col>0</col><colOff>0</colOff><row>9</row>",
        ).replace(
            b"<to><col>3</col><colOff>0</colOff><row>12</row>",
            b"<to><col>1</col><colOff>0</colOff><row>11</row>",
        ),
    )

    inventory = parse_workbook_drawings(path, parse_invoice_bundle(path))
    assert inventory.product_images == ()
    assert inventory.fixed_images[0].anchor.from_row == 9


def test_supports_png_and_jpeg_images_when_content_type_and_magic_match(tmp_path: Path) -> None:
    path = tmp_path / "jpeg.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(
        path,
        "[Content_Types].xml",
        lambda xml: xml.replace(b'Extension="png" ContentType="image/png"', b'Extension="jpg" ContentType="image/jpeg"'),
    )
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/media/image1.jpg"] = _pillow_image_bytes("JPEG")
    parts.pop("xl/media/image1.png")
    parts["xl/drawings/_rels/drawing1.xml.rels"] = parts[
        "xl/drawings/_rels/drawing1.xml.rels"
    ].replace(b"image1.png", b"image1.jpg")
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)

    inventory = parse_workbook_drawings(path, parse_invoice_bundle(path))
    assert inventory.product_images[0].media_part == "xl/media/image1.jpg"


def test_accepts_pillow_generated_png(tmp_path: Path) -> None:
    path = tmp_path / "pillow-png.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(path, "xl/media/image1.png", lambda _: _pillow_image_bytes("PNG"))

    inventory = parse_workbook_drawings(path, parse_invoice_bundle(path))
    assert inventory.product_images[0].media_part == "xl/media/image1.png"


@pytest.mark.parametrize("extension, content_type, payload", [
    ("gif", "image/gif", b"GIF89a"),
    ("svg", "image/svg+xml", b"<svg/>"),
])
def test_rejects_unsupported_image_formats(
    tmp_path: Path, extension: str, content_type: str, payload: bytes
) -> None:
    path = tmp_path / f"unsupported.{extension}.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(
        b'Extension="png" ContentType="image/png"',
        f'Extension="{extension}" ContentType="{content_type}"'.encode(),
    )
    parts[f"xl/media/image1.{extension}"] = payload
    parts.pop("xl/media/image1.png")
    parts["xl/drawings/_rels/drawing1.xml.rels"] = parts[
        "xl/drawings/_rels/drawing1.xml.rels"
    ].replace(b"image1.png", f"image1.{extension}".encode())
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)

    assert _drawing_error(path, "UNSUPPORTED_DRAWING").location == "xl/media/image1." + extension


@pytest.mark.parametrize(
    "part, old, new",
    [
        ("xl/drawings/drawing1.xml", b"twoCellAnchor", b"oneCellAnchor",),
        ("xl/drawings/drawing1.xml", b"pic>", b"graphicFrame>",),
        (
            "xl/drawings/_rels/drawing1.xml.rels",
            b'Target="../media/image1.png"',
            b'Target="https://example.test/image.png" TargetMode="External"',
        ),
    ],
)
def test_rejects_unsupported_anchor_objects_and_external_images(
    tmp_path: Path, part: str, old: bytes, new: bytes
) -> None:
    path = tmp_path / "unsupported.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    count = -1 if old in {b"twoCellAnchor", b"pic>"} else 1
    _rewrite_part(path, part, lambda xml: xml.replace(old, new, count))

    assert _drawing_error(path, "UNSUPPORTED_DRAWING")


def test_rejects_alternate_content_at_any_depth_in_drawing(tmp_path: Path) -> None:
    path = tmp_path / "drawing-alternate-content.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(
        path,
        "xl/drawings/drawing1.xml",
        lambda xml: xml.replace(
            b"<wsDr ",
            b'<wsDr xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" ',
            1,
        ).replace(b"</spPr>", b"<mc:AlternateContent/></spPr>", 1),
    )

    assert _drawing_error(path, "UNSUPPORTED_DRAWING")


def test_rejects_unknown_direct_picture_child(tmp_path: Path) -> None:
    path = tmp_path / "picture-group.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(
        path,
        "xl/drawings/drawing1.xml",
        lambda xml: xml.replace(b"</pic>", b"<grpSp/></pic>", 1),
    )

    assert _drawing_error(path, "UNSUPPORTED_DRAWING")


@pytest.mark.parametrize("nested_child", [b"<grpSp/>", b"<unknown/>", b"<pic/>"])
def test_rejects_unknown_xdr_descendants_inside_shape_properties(
    tmp_path: Path, nested_child: bytes
) -> None:
    path = tmp_path / "nested-picture-object.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(
        path,
        "xl/drawings/drawing1.xml",
        lambda xml: xml.replace(b"</spPr>", nested_child + b"</spPr>", 1),
    )

    assert _drawing_error(path, "UNSUPPORTED_DRAWING")


@pytest.mark.parametrize(
    "nested_xml",
    [
        b'<a:extLst><a:ext><nvPicPr><cNvPr id="2" name="nested"/><cNvPicPr/></nvPicPr></a:ext></a:extLst>',
        b"<a:extLst><a:ext/></a:extLst>",
        b'<foo:unknown xmlns:foo="urn:foreign"/>',
    ],
)
def test_rejects_xdr_or_foreign_wrappers_inside_picture_subtree(
    tmp_path: Path, nested_xml: bytes
) -> None:
    path = tmp_path / "wrapped-picture-object.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(
        path,
        "xl/drawings/drawing1.xml",
        lambda xml: xml.replace(b"</spPr>", nested_xml + b"</spPr>", 1),
    )

    assert _drawing_error(path, "UNSUPPORTED_DRAWING")


def test_accepts_prefixed_worksheet_namespace_for_drawing_lookup(tmp_path: Path) -> None:
    path = tmp_path / "prefixed-worksheet.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])

    def prefix_main_namespace(xml: bytes) -> bytes:
        namespace = b"http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        prefixed = xml.replace(b'xmlns="' + namespace + b'"', b'xmlns:s="' + namespace + b'"', 1)
        return re.sub(
            rb"(<|</)([A-Za-z][A-Za-z0-9]*)",
            lambda match: match.group(1) + b"s:" + match.group(2),
            prefixed,
        )

    _rewrite_part(path, "xl/worksheets/sheet1.xml", prefix_main_namespace)

    assert parse_workbook_drawings(path, parse_invoice_bundle(path)).product_images[0].source_row == 12


def test_rejects_alternate_content_at_any_depth_in_worksheet(tmp_path: Path) -> None:
    path = tmp_path / "worksheet-alternate-content.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(
            b"<worksheet ",
            b'<worksheet xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" ',
            1,
        ).replace(b"</sheetData>", b"<mc:AlternateContent/></sheetData>", 1),
    )

    assert _drawing_error(path, "UNSUPPORTED_DRAWING")


def test_rejects_worksheet_drawing_relationship_without_declaration(tmp_path: Path) -> None:
    path = tmp_path / "undeclared-drawing-relation.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(
        path,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(b'<drawing r:id="rId1"/>', b"", 1),
    )

    assert _drawing_error(path, "INVALID_DRAWING")


def test_rejects_extra_worksheet_drawing_relationship(tmp_path: Path) -> None:
    path = tmp_path / "extra-drawing-relation.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(
        path,
        "xl/worksheets/_rels/sheet1.xml.rels",
        lambda xml: xml.replace(
            b"</Relationships>",
            b'<Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/></Relationships>',
            1,
        ),
    )

    assert _drawing_error(path, "INVALID_DRAWING")


def test_rejects_wrong_drawing_part_content_type(tmp_path: Path) -> None:
    path = tmp_path / "wrong-drawing-content-type.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(
        path,
        "[Content_Types].xml",
        lambda xml: xml.replace(
            b'PartName="/xl/drawings/drawing1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"',
            b'PartName="/xl/drawings/drawing1.xml" ContentType="application/xml"',
            1,
        ),
    )

    assert _drawing_error(path, "INVALID_DRAWING")


@pytest.mark.parametrize(
    "part, old, new, code",
    [
        ("xl/drawings/drawing1.xml", b'r:embed="rId1"', b'r:link="rId1"', "UNSUPPORTED_DRAWING"),
        ("xl/drawings/drawing1.xml", b'name="Synthetic image 1"', b'name=""', "INVALID_DRAWING"),
        (
            "xl/drawings/_rels/drawing1.xml.rels",
            b'</Relationships>',
            b'<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image1.png"/></Relationships>',
            "INVALID_DRAWING",
        ),
        ("[Content_Types].xml", b'ContentType="image/png"', b'ContentType="image/jpeg"', "INVALID_DRAWING"),
    ],
)
def test_rejects_invalid_embeds_duplicate_media_and_content_type_mismatch(
    tmp_path: Path, part: str, old: bytes, new: bytes, code: str
) -> None:
    path = tmp_path / "invalid.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(path, part, lambda xml: xml.replace(old, new, 1))

    assert _drawing_error(path, code)


def test_rejects_orphaned_media_and_allows_a_workbook_without_a_drawing(tmp_path: Path) -> None:
    orphan = tmp_path / "orphan.xlsx"
    build_standard_v1(orphan, {"S1": 1}, [12])
    with ZipFile(orphan, "a", compression=ZIP_DEFLATED) as archive:
        archive.writestr("xl/media/orphan.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
    assert _drawing_error(orphan, "INVALID_DRAWING")

    absent = tmp_path / "absent.xlsx"
    build_standard_v1(absent, {"S1": 1}, [])
    _remove_drawing(absent)
    inventory = parse_workbook_drawings(absent, parse_invoice_bundle(absent))
    assert inventory.product_images == ()
    assert inventory.fixed_images == ()


def test_allows_media_referenced_by_another_drawing_part(tmp_path: Path) -> None:
    path = tmp_path / "second-drawing.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/worksheets/sheet2.xml"] = (
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        b'<drawing r:id="rId1"/></worksheet>'
    )
    parts["xl/worksheets/_rels/sheet2.xml.rels"] = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing2.xml"/>'
        b"</Relationships>"
    )
    parts["xl/drawings/drawing2.xml"] = (
        b'<wsDr xmlns="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        b'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        b"<twoCellAnchor><from><col>0</col><colOff>0</colOff><row>0</row><rowOff>0</rowOff></from>"
        b"<to><col>1</col><colOff>0</colOff><row>1</row><rowOff>0</rowOff></to>"
        b'<pic><nvPicPr><cNvPr id="1" name="Other image"/><cNvPicPr/></nvPicPr>'
        b'<blipFill><a:blip r:embed="rId1"/><a:stretch><a:fillRect/></a:stretch></blipFill>'
        b'<spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></spPr></pic><clientData/></twoCellAnchor>'
        b"</wsDr>"
    )
    parts["xl/drawings/_rels/drawing2.xml.rels"] = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image2.png"/>'
        b"</Relationships>"
    )
    parts["xl/media/image2.png"] = parts["xl/media/image1.png"]
    parts["xl/workbook.xml"] = parts["xl/workbook.xml"].replace(
        b"</sheets>", b'<sheet name="Other" sheetId="2" r:id="rId3"/></sheets>', 1
    )
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        b"</Relationships>",
        b'<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>',
        1,
    )
    parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(
        b"</Types>",
        b'<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/drawings/drawing2.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/></Types>',
        1,
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)

    assert parse_workbook_drawings(path, parse_invoice_bundle(path)).product_images[0].source_row == 12


def test_rejects_one_drawing_part_shared_by_multiple_worksheets(tmp_path: Path) -> None:
    path = tmp_path / "shared-drawing.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/worksheets/sheet2.xml"] = (
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        b'<drawing r:id="rId1"/></worksheet>'
    )
    parts["xl/worksheets/_rels/sheet2.xml.rels"] = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/>'
        b"</Relationships>"
    )
    parts["xl/workbook.xml"] = parts["xl/workbook.xml"].replace(
        b"</sheets>", b'<sheet name="Other" sheetId="2" r:id="rId3"/></sheets>', 1
    )
    parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
        b"</Relationships>",
        b'<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>',
        1,
    )
    parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(
        b"</Types>",
        b'<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        1,
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)

    assert _drawing_error(path, "INVALID_DRAWING")


def test_rejects_unreachable_drawing_image_relationship_from_masking_orphan_media(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unreachable-drawing.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    with ZipFile(path, "a", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/drawings/drawing2.xml",
            b'<wsDr xmlns="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"/>',
        )
        archive.writestr(
            "xl/drawings/_rels/drawing2.xml.rels",
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image2.png"/>'
            b"</Relationships>",
        )
        archive.writestr(
            "xl/media/image2.png",
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02",
        )

    assert _drawing_error(path, "INVALID_DRAWING")


@pytest.mark.parametrize("target", ["../media/./image1.png", "/xl/media/image1.png", "../media/IMAGE1.PNG"])
def test_resolves_relative_root_relative_and_case_insensitive_media_targets(
    tmp_path: Path, target: str
) -> None:
    path = tmp_path / "target.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(
        path,
        "xl/drawings/_rels/drawing1.xml.rels",
        lambda xml: xml.replace(b"../media/image1.png", target.encode()),
    )

    assert parse_workbook_drawings(path, parse_invoice_bundle(path)).product_images[0].source_row == 12


@pytest.mark.parametrize(
    "part, rewrite",
    [
        (
            "xl/drawings/drawing1.xml",
            lambda xml: xml.replace(b'r:embed="rId1"', b'r:embed="rId2" r:embed="rId1"'),
        ),
        (
            "xl/drawings/drawing1.xml",
            lambda xml: xml.replace(b'<a:blip r:embed="rId1"/>', b"<a:blip/>"),
        ),
        (
            "xl/drawings/_rels/drawing1.xml.rels",
            lambda xml: xml.replace(b"../media/image1.png", b"../media/missing.png"),
        ),
        (
            "xl/drawings/drawing1.xml",
            lambda xml: xml.replace(b"<clientData/>", b"<clientData/><clientData/>"),
        ),
    ],
)
def test_rejects_missing_or_duplicate_picture_contracts(
    tmp_path: Path, part: str, rewrite: Callable[[bytes], bytes]
) -> None:
    path = tmp_path / "broken-contract.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(path, part, rewrite)

    assert _drawing_error(path, "INVALID_DRAWING")


def test_rejects_duplicate_picture_ids_and_embeds(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-ids.xlsx"
    build_standard_v1(path, {"S1": 2}, [12, 13])
    _rewrite_part(
        path,
        "xl/drawings/drawing1.xml",
        lambda xml: xml.replace(b'id="2" name="Synthetic image 2"', b'id="1" name="Synthetic image 1"').replace(
            b'r:embed="rId2"', b'r:embed="rId1"'
        ),
    )

    assert _drawing_error(path, "INVALID_DRAWING")


def test_allows_duplicate_picture_display_names_with_distinct_identities(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-display-names.xlsx"
    build_standard_v1(path, {"S1": 2}, [12, 13])
    _rewrite_part(
        path,
        "xl/drawings/drawing1.xml",
        lambda xml: xml.replace(b'name="Synthetic image 2"', b'name="Synthetic image 1"'),
    )

    inventory = parse_workbook_drawings(path, parse_invoice_bundle(path))

    assert [image.relationship_id for image in inventory.product_images] == ["rId1", "rId2"]


def test_rejects_bad_png_magic_and_a_chart_anchor(tmp_path: Path) -> None:
    magic = tmp_path / "bad-magic.xlsx"
    build_standard_v1(magic, {"S1": 1}, [12])
    _rewrite_part(magic, "xl/media/image1.png", lambda _: b"not a PNG")
    assert _drawing_error(magic, "INVALID_DRAWING").location == "xl/media/image1.png"

    chart = tmp_path / "chart.xlsx"
    build_standard_v1(chart, {"S1": 1}, [12])
    _rewrite_part(
        chart,
        "xl/drawings/drawing1.xml",
        lambda xml: xml.replace(b"pic>", b"graphicFrame>"),
    )
    assert _drawing_error(chart, "UNSUPPORTED_DRAWING")


def test_rejects_truncated_png_and_jpeg_payloads(tmp_path: Path) -> None:
    png = tmp_path / "truncated-png.xlsx"
    build_standard_v1(png, {"S1": 1}, [12])
    _rewrite_part(
        png,
        "xl/media/image1.png",
        lambda _: b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 17,
    )
    assert _drawing_error(png, "INVALID_DRAWING")

    jpeg = tmp_path / "truncated-jpeg.xlsx"
    build_standard_v1(jpeg, {"S1": 1}, [12])
    _rewrite_part(jpeg, "xl/media/image1.png", lambda _: _JPEG_1X1[:-2])
    assert _drawing_error(jpeg, "INVALID_DRAWING")


def test_rejects_structurally_valid_but_undecodable_media(tmp_path: Path) -> None:
    png = tmp_path / "undecodable-png.xlsx"
    build_standard_v1(png, {"S1": 1}, [12])
    png_payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", b"not-zlib-data")
        + _png_chunk(b"IEND", b"")
    )
    _rewrite_part(png, "xl/media/image1.png", lambda _: png_payload)
    assert _drawing_error(png, "UNSUPPORTED_DRAWING").location == "xl/media/image1.png"

    jpeg = tmp_path / "undecodable-jpeg.xlsx"
    build_standard_v1(jpeg, {"S1": 1}, [12])
    with ZipFile(jpeg) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(
        b'Extension="png" ContentType="image/png"',
        b'Extension="jpg" ContentType="image/jpeg"',
    )
    parts["xl/media/image1.jpg"] = (
        b"\xff\xd8"
        b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x00\xff\xd9"
    )
    parts.pop("xl/media/image1.png")
    parts["xl/drawings/_rels/drawing1.xml.rels"] = parts[
        "xl/drawings/_rels/drawing1.xml.rels"
    ].replace(b"image1.png", b"image1.jpg")
    with ZipFile(jpeg, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    assert _drawing_error(jpeg, "UNSUPPORTED_DRAWING").location == "xl/media/image1.jpg"


def test_rejects_png_ztxt_decompression_bomb(tmp_path: Path) -> None:
    path = tmp_path / "png-ztxt-bomb.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    payload = _pillow_image_bytes("PNG")
    ihdr_end = 8 + 12 + 13
    ztxt = _png_chunk(
        b"zTXt",
        b"comment\x00\x00" + zlib.compress(b"x" * 1_048_577),
    )
    payload = payload[:ihdr_end] + ztxt + payload[ihdr_end:]
    _rewrite_part(path, "xl/media/image1.png", lambda _: payload)

    assert _drawing_error(path, "UNSUPPORTED_DRAWING").location == "xl/media/image1.png"


def test_content_types_are_read_once_and_rejected_after_package_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "content-type-cache.xlsx"
    build_standard_v1(path, {"S1": 2}, [12, 13])
    reads = 0
    original_read_xml = SafePackage.read_xml

    def counted_read_xml(package: SafePackage, part: str, max_bytes: int):
        nonlocal reads
        if part == "[Content_Types].xml":
            reads += 1
        return original_read_xml(package, part, max_bytes)

    monkeypatch.setattr(SafePackage, "read_xml", counted_read_xml)
    parsed_bundle = parse_invoice_bundle(path)
    reads = 0
    parse_workbook_drawings(path, parsed_bundle)
    assert reads == 1

    with SafePackage.open(path) as package:
        package.content_type_for_part("xl/media/image1.png")
        package.close()
        with pytest.raises(WorkbookError) as captured:
            package.content_type_for_part("xl/media/image1.png")
        assert captured.value.code == "PACKAGE_CLOSED"


def test_rejects_anchor_offsets_outside_the_supported_product_boundary(tmp_path: Path) -> None:
    path = tmp_path / "offset.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(
        path,
        "xl/drawings/drawing1.xml",
        lambda xml: xml.replace(
            b"<to><col>3</col><colOff>0</colOff><row>12</row><rowOff>0</rowOff>",
            b"<to><col>3</col><colOff>0</colOff><row>12</row><rowOff>1</rowOff>",
            1,
        ),
    )

    assert _drawing_error(path, "AMBIGUOUS_IMAGE").location == "xl/drawings/drawing1.xml:rId1"


def test_rejects_nonzero_offsets_on_fixed_images(tmp_path: Path) -> None:
    path = tmp_path / "fixed-offset.xlsx"
    build_standard_v1(path, {"S1": 1}, [12])
    _rewrite_part(
        path,
        "xl/drawings/drawing1.xml",
        lambda xml: xml.replace(
            b"<from><col>2</col><colOff>0</colOff><row>11</row><rowOff>0</rowOff>",
            b"<from><col>0</col><colOff>1</colOff><row>9</row><rowOff>0</rowOff>",
            1,
        ).replace(
            b"<to><col>3</col><colOff>0</colOff><row>12</row><rowOff>0</rowOff>",
            b"<to><col>1</col><colOff>0</colOff><row>11</row><rowOff>0</rowOff>",
            1,
        ),
    )

    assert _drawing_error(path, "INVALID_DRAWING")
