"""Validate and classify the narrowly supported FIS V1 worksheet images."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from lxml import etree  # type: ignore[import-untyped]

from fis.xlsx.errors import WorkbookError
from fis.xlsx.package import MAX_ENTRY, SafePackage
from fis.xlsx.parser import (
    MAIN_NS,
    MAX_EXCEL_COLUMN,
    MAX_EXCEL_ROW,
    OFFICE_REL_NS,
    PACKAGE_REL_NS,
    ParsedBundle,
    _workbook_relationships,
    _worksheet_part,
)

XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
DRAWING_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.drawing+xml"
IMAGE_RELATIONSHIP_TYPES = frozenset(
    {
        "http://schemas.openxmlformats.org/officedocument/2006/relationships/image",
        "http://purl.oclc.org/ooxml/officedocument/relationships/image",
    }
)
DRAWING_RELATIONSHIP_TYPES = frozenset(
    {
        "http://schemas.openxmlformats.org/officedocument/2006/relationships/drawing",
        "http://purl.oclc.org/ooxml/officedocument/relationships/drawing",
    }
)
WORKSHEET_RELATIONSHIP_TYPES = frozenset(
    {
        "http://schemas.openxmlformats.org/officedocument/2006/relationships/worksheet",
        "http://purl.oclc.org/ooxml/officedocument/relationships/worksheet",
    }
)
_MAX_OFFSET = 2_147_483_647
_SUPPORTED_MEDIA = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


@dataclass(frozen=True, slots=True)
class Anchor:
    """A two-cell anchor using the OOXML zero-based row and column values."""

    from_row: int
    from_col: int
    from_row_off: int
    from_col_off: int
    to_row: int
    to_col: int
    to_row_off: int
    to_col_off: int
    relationship_id: str


@dataclass(frozen=True, slots=True)
class ProductImage:
    """One supported product picture, bound to a one-based source row."""

    source_row: int
    relationship_id: str
    media_part: str
    anchor: Anchor


@dataclass(frozen=True, slots=True)
class FixedImage:
    """One supported non-product picture retained by later workbook transforms."""

    relationship_id: str
    media_part: str
    anchor: Anchor


@dataclass(frozen=True, slots=True)
class DrawingInventory:
    """Deeply immutable image metadata required to reproduce worksheet drawings."""

    drawing_part: str | None
    worksheet_relationship_id: str | None
    product_images: tuple[ProductImage, ...]
    fixed_images: tuple[FixedImage, ...]
    relationships: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "product_images", tuple(self.product_images))
        object.__setattr__(self, "fixed_images", tuple(self.fixed_images))
        object.__setattr__(self, "relationships", MappingProxyType(dict(self.relationships)))


@dataclass(frozen=True, slots=True)
class _ParsedDrawing:
    drawing_part: str
    parsed: tuple[tuple[Anchor, str, str], ...]
    media_by_id: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parsed", tuple(self.parsed))
        object.__setattr__(self, "media_by_id", MappingProxyType(dict(self.media_by_id)))


def _drawing_error(code: str, location: str) -> WorkbookError:
    messages = {
        "AMBIGUOUS_IMAGE": "图片无法唯一归属到一个产品行",
        "UNSUPPORTED_DRAWING": "V1 不支持该工作表图像或绘图对象",
        "INVALID_DRAWING": "工作表图像结构或关系无效",
    }
    return WorkbookError(code, messages[code], location)


def _anchor_rows(anchor: Anchor) -> range:
    """Return worksheet rows actually occupied; a zero end offset ends at a boundary."""

    final_row = anchor.to_row + 1
    if anchor.to_row_off == 0:
        final_row -= 1
    return range(anchor.from_row + 1, final_row + 1)


def _valid_coordinate(value: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum


def _validate_anchor(anchor: Anchor, location: str) -> None:
    if not all(
        (
            _valid_coordinate(anchor.from_row, MAX_EXCEL_ROW - 1),
            _valid_coordinate(anchor.to_row, MAX_EXCEL_ROW - 1),
            _valid_coordinate(anchor.from_col, MAX_EXCEL_COLUMN - 1),
            _valid_coordinate(anchor.to_col, MAX_EXCEL_COLUMN - 1),
            _valid_coordinate(anchor.from_row_off, _MAX_OFFSET),
            _valid_coordinate(anchor.from_col_off, _MAX_OFFSET),
            _valid_coordinate(anchor.to_row_off, _MAX_OFFSET),
            _valid_coordinate(anchor.to_col_off, _MAX_OFFSET),
        )
    ):
        raise _drawing_error("INVALID_DRAWING", location)
    if (anchor.to_row, anchor.to_row_off) < (anchor.from_row, anchor.from_row_off) or (
        anchor.to_col,
        anchor.to_col_off,
    ) < (anchor.from_col, anchor.from_col_off):
        raise _drawing_error("INVALID_DRAWING", location)


def classify_anchor(
    anchor: Anchor, product_rows: tuple[int, ...] | list[int] | set[int], image_column: int = 3
) -> int | None:
    """Return a product source row, or ``None`` for a fixed image.

    The only supported product picture begins in the image column on its product
    row and ends no later than the next row boundary.  All other valid anchors
    must stay entirely outside the product region and are treated as fixed art.
    """

    location = anchor.relationship_id or "anchor"
    _validate_anchor(anchor, location)
    if not isinstance(image_column, int) or isinstance(image_column, bool):
        raise TypeError("image_column must be an integer")
    product_row_set = set(product_rows)
    if any(not isinstance(row, int) or isinstance(row, bool) or row < 1 for row in product_row_set):
        raise ValueError("product_rows must contain positive row numbers")

    source_row = anchor.from_row + 1
    if source_row in product_row_set:
        if anchor.from_col + 1 != image_column:
            raise _drawing_error("AMBIGUOUS_IMAGE", location)
        if anchor.to_row > anchor.from_row + 1 or (
            anchor.to_row == anchor.from_row + 1 and anchor.to_row_off != 0
        ):
            raise _drawing_error("AMBIGUOUS_IMAGE", location)
        return source_row

    if any(row in product_row_set for row in _anchor_rows(anchor)):
        raise _drawing_error("AMBIGUOUS_IMAGE", location)
    return None


def _relationship_part(source_part: str) -> str:
    source = PurePosixPath(source_part)
    return str(source.parent / "_rels" / f"{source.name}.rels")


def _relationships(package: SafePackage, source_part: str, location: str) -> list[etree._Element]:
    return _relationships_optional(package, source_part, location, required=True)


def _relationships_optional(
    package: SafePackage, source_part: str, location: str, *, required: bool
) -> list[etree._Element]:
    try:
        root = package.read_xml(_relationship_part(source_part), MAX_ENTRY)
    except WorkbookError as exc:
        if not required and exc.code == "MISSING_PART":
            return []
        raise _drawing_error("INVALID_DRAWING", location) from exc
    if root.tag != f"{{{PACKAGE_REL_NS}}}Relationships":
        raise _drawing_error("INVALID_DRAWING", location)
    relations = list(root)
    if any(item.tag != f"{{{PACKAGE_REL_NS}}}Relationship" for item in relations):
        raise _drawing_error("INVALID_DRAWING", location)
    return relations


def _unique_relationship(
    relationships: list[etree._Element], relationship_id: str, relationship_type: set[str] | frozenset[str], location: str
) -> etree._Element:
    matches = [item for item in relationships if item.get("Id") == relationship_id]
    if len(matches) != 1 or matches[0].get("Type", "").lower() not in relationship_type:
        raise _drawing_error("INVALID_DRAWING", location)
    return matches[0]


def _integer_child(parent: etree._Element, name: str, location: str) -> int:
    values = parent.findall(f"{{{XDR_NS}}}{name}")
    if len(values) != 1 or values[0].text is None:
        raise _drawing_error("INVALID_DRAWING", location)
    try:
        value = int(values[0].text)
    except ValueError as exc:
        raise _drawing_error("INVALID_DRAWING", location) from exc
    if str(value) != values[0].text or value < 0:
        raise _drawing_error("INVALID_DRAWING", location)
    return value


def _anchor_cell(parent: etree._Element, location: str) -> tuple[int, int, int, int]:
    expected = [f"{{{XDR_NS}}}{name}" for name in ("col", "colOff", "row", "rowOff")]
    if [child.tag for child in parent] != expected:
        raise _drawing_error("INVALID_DRAWING", location)
    return tuple(_integer_child(parent, name, location) for name in ("row", "col", "rowOff", "colOff"))  # type: ignore[return-value]


_PIC_XDR_CHILDREN = {
    "pic": {"nvPicPr", "blipFill", "spPr", "style", "extLst"},
    "nvPicPr": {"cNvPr", "cNvPicPr", "nvPr", "extLst"},
    "cNvPr": set(),
    "cNvPicPr": set(),
    "nvPr": {"extLst"},
    "blipFill": set(),
    "spPr": set(),
    "style": set(),
    "extLst": {"ext"},
    "ext": set(),
}


def _validate_picture_subtree(picture: etree._Element, location: str) -> None:
    """Reject spreadsheetDrawing objects hidden inside an otherwise valid picture."""

    for element in picture.iter():
        if not isinstance(element.tag, str) or not element.tag.startswith("{"):
            continue
        namespace, _, local_name = element.tag[1:].partition("}")
        if namespace != XDR_NS:
            continue
        allowed_children = _PIC_XDR_CHILDREN.get(local_name)
        if allowed_children is None:
            raise _drawing_error("UNSUPPORTED_DRAWING", location)
        for child in element:
            if not isinstance(child.tag, str) or not child.tag.startswith("{"):
                continue
            child_namespace, _, child_local_name = child.tag[1:].partition("}")
            if child_namespace == XDR_NS and child_local_name not in allowed_children:
                raise _drawing_error("UNSUPPORTED_DRAWING", location)
        if local_name == "nvPicPr":
            xdr_children = [
                child.tag[1:].partition("}")[2]
                for child in element
                if isinstance(child.tag, str)
                and child.tag.startswith("{")
                and child.tag[1:].partition("}")[0] == XDR_NS
            ]
            if (
                xdr_children[:2] != ["cNvPr", "cNvPicPr"]
                or xdr_children.count("cNvPr") != 1
                or xdr_children.count("cNvPicPr") != 1
            ):
                raise _drawing_error("INVALID_DRAWING", location)


def _parse_anchor(anchor_element: etree._Element, drawing_part: str, number: int) -> tuple[Anchor, str, str]:
    location = f"{drawing_part}:anchor-{number}"
    expected = [
        f"{{{XDR_NS}}}from",
        f"{{{XDR_NS}}}to",
        f"{{{XDR_NS}}}pic",
        f"{{{XDR_NS}}}clientData",
    ]
    children = [child.tag for child in anchor_element]
    if any(child not in expected for child in children):
        raise _drawing_error("UNSUPPORTED_DRAWING", location)
    if children != expected:
        raise _drawing_error("INVALID_DRAWING", location)
    from_row, from_col, from_row_off, from_col_off = _anchor_cell(anchor_element[0], location)
    to_row, to_col, to_row_off, to_col_off = _anchor_cell(anchor_element[1], location)
    picture = anchor_element[2]
    _validate_picture_subtree(picture, location)
    picture_children = [child.tag for child in picture]
    required_picture_children = [
        f"{{{XDR_NS}}}nvPicPr",
        f"{{{XDR_NS}}}blipFill",
        f"{{{XDR_NS}}}spPr",
    ]
    optional_picture_children = {
        f"{{{XDR_NS}}}style",
        f"{{{XDR_NS}}}extLst",
    }
    if any(child not in required_picture_children + list(optional_picture_children) for child in picture_children):
        raise _drawing_error("UNSUPPORTED_DRAWING", location)
    if picture_children[:3] != required_picture_children or len(
        [child for child in picture_children if child in required_picture_children]
    ) != 3:
        raise _drawing_error("INVALID_DRAWING", location)
    if any(picture_children.count(child) > 1 for child in optional_picture_children):
        raise _drawing_error("INVALID_DRAWING", location)
    if any(child not in optional_picture_children for child in picture_children[3:]):
        raise _drawing_error("UNSUPPORTED_DRAWING", location)
    non_visual = picture.findall(f"{{{XDR_NS}}}nvPicPr/{{{XDR_NS}}}cNvPr")
    if len(non_visual) != 1:
        raise _drawing_error("INVALID_DRAWING", location)
    picture_id = non_visual[0].get("id", "")
    picture_name = non_visual[0].get("name", "")
    try:
        valid_id = int(picture_id) > 0 and str(int(picture_id)) == picture_id
    except ValueError:
        valid_id = False
    if not valid_id or not picture_name.strip():
        raise _drawing_error("INVALID_DRAWING", location)
    blips = picture.findall(f"{{{XDR_NS}}}blipFill/{{{DRAWING_NS}}}blip")
    all_blips = picture.findall(f".//{{{DRAWING_NS}}}blip")
    if len(blips) != 1 or len(all_blips) != 1:
        raise _drawing_error("INVALID_DRAWING", location)
    blip = blips[0]
    relationship_id = blip.get(f"{{{OFFICE_REL_NS}}}embed")
    if blip.get(f"{{{OFFICE_REL_NS}}}link") is not None:
        raise _drawing_error("UNSUPPORTED_DRAWING", location)
    if not relationship_id:
        raise _drawing_error("INVALID_DRAWING", location)
    if len(blip.attrib) != 1:
        raise _drawing_error("UNSUPPORTED_DRAWING", location)
    anchor = Anchor(
        from_row,
        from_col,
        from_row_off,
        from_col_off,
        to_row,
        to_col,
        to_row_off,
        to_col_off,
        relationship_id,
    )
    _validate_anchor(anchor, f"{drawing_part}:{relationship_id}")
    return anchor, picture_id, picture_name


def _validate_media(package: SafePackage, media_part: str) -> None:
    suffix = PurePosixPath(media_part).suffix.removeprefix(".").lower()
    if suffix not in _SUPPORTED_MEDIA:
        raise _drawing_error("UNSUPPORTED_DRAWING", media_part)
    try:
        content_type = package.content_type_for_part(media_part)
    except WorkbookError as exc:
        raise _drawing_error("INVALID_DRAWING", media_part) from exc
    if content_type != _SUPPORTED_MEDIA[suffix]:
        raise _drawing_error("INVALID_DRAWING", media_part)
    payload = package.read(media_part, MAX_ENTRY)
    if suffix == "png":
        valid = (
            len(payload) >= 33
            and payload.startswith(b"\x89PNG\r\n\x1a\n")
            and payload[8:12] == b"\x00\x00\x00\r"
            and payload[12:16] == b"IHDR"
        )
    else:
        valid = len(payload) >= 4 and payload.startswith(b"\xff\xd8") and payload.endswith(b"\xff\xd9")
    if not valid:
        raise _drawing_error("INVALID_DRAWING", media_part)


def _empty_inventory() -> DrawingInventory:
    return DrawingInventory(None, None, (), (), {})


def _contains_alternate_content(root: etree._Element) -> bool:
    return any(element.tag == f"{{{MC_NS}}}AlternateContent" for element in root.iter())


def _worksheet_drawing_parts(
    package: SafePackage,
) -> list[tuple[str, str, str]]:
    """Return every drawing declared by every workbook worksheet."""

    try:
        workbook = package.read_xml("xl/workbook.xml", MAX_ENTRY)
        workbook_relations = _relationships(package, "xl/workbook.xml", "xl/workbook.xml")
    except WorkbookError as exc:
        raise _drawing_error("INVALID_DRAWING", "xl/workbook.xml") from exc
    if workbook.tag != f"{{{MAIN_NS}}}workbook":
        raise _drawing_error("INVALID_DRAWING", "xl/workbook.xml")
    sheets = workbook.findall(f"{{{MAIN_NS}}}sheets/{{{MAIN_NS}}}sheet")
    records: list[tuple[str, str, str]] = []
    for sheet in sheets:
        relationship_id = sheet.get(f"{{{OFFICE_REL_NS}}}id")
        if not relationship_id:
            raise _drawing_error("INVALID_DRAWING", "xl/workbook.xml")
        worksheet_relation = _unique_relationship(
            workbook_relations, relationship_id, WORKSHEET_RELATIONSHIP_TYPES, "xl/workbook.xml"
        )
        target = worksheet_relation.get("Target")
        if not target:
            raise _drawing_error("INVALID_DRAWING", "xl/workbook.xml")
        try:
            worksheet_part = package.resolve_relationship_target("xl/workbook.xml", target)
            worksheet = package.read_xml(worksheet_part, MAX_ENTRY)
        except WorkbookError as exc:
            raise _drawing_error("INVALID_DRAWING", "xl/workbook.xml") from exc
        if worksheet.tag != f"{{{MAIN_NS}}}worksheet":
            raise _drawing_error("INVALID_DRAWING", worksheet_part)
        if _contains_alternate_content(worksheet):
            raise _drawing_error("UNSUPPORTED_DRAWING", worksheet_part)

        drawing_elements = worksheet.findall(f"{{{MAIN_NS}}}drawing")
        worksheet_relations = _relationships_optional(
            package, worksheet_part, worksheet_part, required=False
        )
        drawing_relations = [
            relation
            for relation in worksheet_relations
            if relation.get("Type", "").lower() in DRAWING_RELATIONSHIP_TYPES
        ]
        if not drawing_elements:
            if drawing_relations:
                raise _drawing_error("INVALID_DRAWING", worksheet_part)
            continue
        if len(drawing_elements) != 1 or len(drawing_relations) != 1:
            raise _drawing_error("INVALID_DRAWING", worksheet_part)
        drawing_id = drawing_elements[0].get(f"{{{OFFICE_REL_NS}}}id")
        if not drawing_id:
            raise _drawing_error("INVALID_DRAWING", worksheet_part)
        drawing_relation = _unique_relationship(
            worksheet_relations, drawing_id, DRAWING_RELATIONSHIP_TYPES, worksheet_part
        )
        if drawing_relation is not drawing_relations[0]:
            raise _drawing_error("INVALID_DRAWING", worksheet_part)
        target = drawing_relation.get("Target")
        if not target:
            raise _drawing_error("INVALID_DRAWING", worksheet_part)
        try:
            drawing_part = package.resolve_relationship_target(worksheet_part, target)
        except WorkbookError as exc:
            raise _drawing_error("INVALID_DRAWING", worksheet_part) from exc
        records.append((worksheet_part, drawing_id, drawing_part))
    return records


def _parse_drawing_part(package: SafePackage, drawing_part: str) -> _ParsedDrawing:
    try:
        drawing_content_type = package.content_type_for_part(drawing_part)
    except WorkbookError as exc:
        raise _drawing_error("INVALID_DRAWING", drawing_part) from exc
    if drawing_content_type != DRAWING_CONTENT_TYPE:
        raise _drawing_error("INVALID_DRAWING", drawing_part)
    try:
        drawing = package.read_xml(drawing_part, MAX_ENTRY)
    except WorkbookError as exc:
        raise _drawing_error("INVALID_DRAWING", drawing_part) from exc
    if drawing.tag != f"{{{XDR_NS}}}wsDr":
        raise _drawing_error("UNSUPPORTED_DRAWING", drawing_part)
    if _contains_alternate_content(drawing):
        raise _drawing_error("UNSUPPORTED_DRAWING", drawing_part)

    drawing_relations = _relationships(package, drawing_part, drawing_part)
    relation_by_id: dict[str, etree._Element] = {}
    for relation in drawing_relations:
        relationship_id = relation.get("Id")
        if not relationship_id or relationship_id in relation_by_id:
            raise _drawing_error("INVALID_DRAWING", drawing_part)
        if relation.get("Type", "").lower() not in IMAGE_RELATIONSHIP_TYPES:
            raise _drawing_error("UNSUPPORTED_DRAWING", f"{drawing_part}:{relationship_id}")
        relation_by_id[relationship_id] = relation

    picture_ids: set[str] = set()
    picture_names: set[str] = set()
    embedded_ids: set[str] = set()
    parsed: list[tuple[Anchor, str, str]] = []
    for number, child in enumerate(drawing, start=1):
        if child.tag != f"{{{XDR_NS}}}twoCellAnchor":
            raise _drawing_error("UNSUPPORTED_DRAWING", f"{drawing_part}:anchor-{number}")
        anchor, picture_id, picture_name = _parse_anchor(child, drawing_part, number)
        if (
            picture_id in picture_ids
            or picture_name in picture_names
            or anchor.relationship_id in embedded_ids
        ):
            raise _drawing_error("INVALID_DRAWING", f"{drawing_part}:{anchor.relationship_id}")
        picture_ids.add(picture_id)
        picture_names.add(picture_name)
        embedded_ids.add(anchor.relationship_id)
        parsed.append((anchor, picture_id, picture_name))
    if set(relation_by_id) != embedded_ids:
        raise _drawing_error("INVALID_DRAWING", drawing_part)

    media_by_id: dict[str, str] = {}
    for relationship_id, relation in relation_by_id.items():
        target = relation.get("Target")
        if not target:
            raise _drawing_error("INVALID_DRAWING", f"{drawing_part}:{relationship_id}")
        try:
            media_part = package.resolve_relationship_target(drawing_part, target)
        except WorkbookError as exc:
            raise _drawing_error("INVALID_DRAWING", f"{drawing_part}:{relationship_id}") from exc
        if media_part in media_by_id.values():
            raise _drawing_error("INVALID_DRAWING", f"{drawing_part}:{relationship_id}")
        media_by_id[relationship_id] = media_part
    for media_part in media_by_id.values():
        _validate_media(package, media_part)
    return _ParsedDrawing(drawing_part, tuple(parsed), media_by_id)


def _all_reachable_media(package: SafePackage) -> tuple[dict[str, tuple[str, _ParsedDrawing]], set[str]]:
    records: dict[str, tuple[str, _ParsedDrawing]] = {}
    media_parts: set[str] = set()
    for worksheet_part, drawing_id, drawing_part in _worksheet_drawing_parts(package):
        parsed_drawing = _parse_drawing_part(package, drawing_part)
        records[worksheet_part] = (drawing_id, parsed_drawing)
        media_parts.update(parsed_drawing.media_by_id.values())
    return records, media_parts


def _parse_inventory(package: SafePackage, parsed_bundle: ParsedBundle) -> DrawingInventory:
    worksheet_part, _ = _worksheet_part(package, _workbook_relationships(package))
    drawing_records, reachable_media = _all_reachable_media(package)
    all_media = {name.lower() for name in package.names if name.lower().startswith("xl/media/")}
    if all_media != {target.lower() for target in reachable_media}:
        raise _drawing_error("INVALID_DRAWING", worksheet_part)
    selected = drawing_records.get(worksheet_part)
    if selected is None:
        return _empty_inventory()
    drawing_id, drawing = selected
    drawing_part = drawing.drawing_part
    parsed = drawing.parsed
    media_by_id = drawing.media_by_id

    product_images: list[ProductImage] = []
    fixed_images: list[FixedImage] = []
    seen_product_rows: set[int] = set()
    for anchor, _, _ in parsed:
        location = f"{drawing_part}:{anchor.relationship_id}"
        try:
            source_row = classify_anchor(anchor, parsed_bundle.product_rows)
        except WorkbookError as exc:
            raise WorkbookError(exc.code, exc.message, location) from exc
        media_part = media_by_id[anchor.relationship_id]
        if source_row is None:
            fixed_images.append(FixedImage(anchor.relationship_id, media_part, anchor))
        elif source_row in seen_product_rows:
            raise _drawing_error("AMBIGUOUS_IMAGE", location)
        else:
            seen_product_rows.add(source_row)
            product_images.append(ProductImage(source_row, anchor.relationship_id, media_part, anchor))
    return DrawingInventory(
        drawing_part,
        drawing_id,
        tuple(product_images),
        tuple(fixed_images),
        media_by_id,
    )


def parse_workbook_drawings(path: Path | str, parsed_bundle: ParsedBundle) -> DrawingInventory:
    """Load the configured worksheet's supported images from a safe XLSX package."""

    try:
        with SafePackage.open(path) as package:
            return _parse_inventory(package, parsed_bundle)
    except WorkbookError as exc:
        if exc.code == "EXTERNAL_RESOURCE":
            raise _drawing_error("UNSUPPORTED_DRAWING", exc.location or "relationship") from exc
        if exc.code in {
            "INVALID_RELATIONSHIP",
            "MISSING_RELATIONSHIP_SOURCE",
            "MISSING_RELATIONSHIP_TARGET",
        }:
            raise _drawing_error("INVALID_DRAWING", exc.location or "relationship") from exc
        raise
