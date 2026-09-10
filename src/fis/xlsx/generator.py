"""Generate one preserved OOXML workbook for a selected Shipment."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import cast
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from lxml import etree  # type: ignore[import-untyped]

from fis.xlsx.drawings import parse_workbook_drawings
from fis.xlsx.errors import WorkbookError
from fis.xlsx.package import MAX_ENTRY, SafePackage
from fis.xlsx.parser import (
    _workbook_relationships,
    _worksheet_part,
    parse_invoice_bundle,
)
from fis.xlsx.template import STANDARD_V1
from fis.xlsx.transform import (
    build_row_map,
    transform_comments_vml_xml,
    transform_comments_xml,
    transform_drawing_anchors_xml,
    transform_sheet_xml,
    transform_workbook_xml,
)

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
DRAWING_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"
COMMENTS_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
VML_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/vmlDrawing"
IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"


@dataclass(frozen=True, slots=True)
class ShipmentOutput:
    """Confirmed values written into one generated Shipment workbook."""

    shipment_id: str
    warehouse_code: str
    warehouse_address: str
    channel: str
    delivery_date: date
    sailing_date: date
    delivery_warehouse: str


@dataclass(frozen=True, slots=True)
class _GeneratedPayload:
    payload: bytes
    expected_rows: int
    expected_images: int
    expected_fixed_images: int
    expected_merge_count: int
    expected_print_area: bool
    expected_unchanged_parts: Mapping[str, str]
    expected_row_attributes: Mapping[int, Mapping[str, str]]
    expected_cols_xml: bytes | None
    expected_fixed_media: tuple[str, ...]
    expected_fixed_anchors: tuple[tuple[int, int, int, int, int, int, int, int], ...]


def safe_filename(request: ShipmentOutput) -> str:
    """Return a deterministic filename without allowing path separators."""

    clean = re.compile(r"[^A-Za-z0-9._-]+")
    shipment = clean.sub("_", request.shipment_id).strip("._")
    warehouse = clean.sub("_", request.warehouse_code).strip("._")
    if not shipment or not warehouse or shipment in {".", ".."} or warehouse in {".", ".."}:
        raise WorkbookError("INVALID_FILENAME", "货件或仓库编码无法生成安全文件名")
    return f"{shipment}_{warehouse}.xlsx"


def generate_shipment_workbook(
    source: Path | str,
    request: ShipmentOutput,
    output_dir: Path | str,
) -> Path:
    """Generate and independently validate one workbook, publishing atomically."""

    source_path = Path(source)
    destination = Path(output_dir)
    output_path = destination if destination.suffix.lower() == ".xlsx" else destination / safe_filename(request)
    if _same_path(source_path, output_path):
        raise WorkbookError("OUTPUT_PATH_CONFLICT", "输出文件不能覆盖源工作簿", str(output_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    generated = _generate_payload(source_path, request)
    payload = generated.payload
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{output_path.stem}.", suffix=".xlsx", dir=output_path.parent, delete=False
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        report = validate_generated_workbook(
            temp_path,
            expected_shipment=request.shipment_id,
            expected_rows=generated.expected_rows,
            expected_images=generated.expected_images,
            expected_fixed_images=generated.expected_fixed_images,
            expected_merge_count=generated.expected_merge_count,
            expected_print_area=generated.expected_print_area,
            expected_row_attributes=generated.expected_row_attributes,
            expected_cols_xml=generated.expected_cols_xml,
            expected_fixed_media=generated.expected_fixed_media,
            expected_fixed_anchors=generated.expected_fixed_anchors,
            expected_values=_request_values(request),
            expected_unchanged_parts=generated.expected_unchanged_parts,
        )
        if not report.ok:
            raise WorkbookError("OUTPUT_VALIDATION_FAILED", report.error or "生成工作簿校验失败")
        os.replace(temp_path, output_path)
        temp_path = None
        return output_path
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def generate_shipment_workbook_bytes(source: Path | str | bytes, request: ShipmentOutput) -> bytes:
    """Generate a validated workbook and return its deterministic ZIP bytes."""

    if isinstance(source, bytes):
        with tempfile.TemporaryDirectory(prefix="fis-source-") as directory:
            source_path = Path(directory) / "source.xlsx"
            source_path.write_bytes(source)
            generated = _generate_payload(source_path, request)
            return _validated_payload(generated, request)
    return _validated_payload(_generate_payload(Path(source), request), request)


def generate_shipment_workbooks(
    source: Path | str, requests: tuple[ShipmentOutput, ...], output_dir: Path | str
) -> tuple[Path, ...]:
    """Generate a complete batch after checking normalized filename collisions."""

    names = [safe_filename(request) for request in requests]
    if len({name.casefold() for name in names}) != len(names):
        raise WorkbookError("DUPLICATE_OUTPUT_FILENAME", "多个货件生成了相同的文件名", "output")
    return tuple(
        generate_shipment_workbook(source, request, output_dir)
        for request in requests
    )


def _same_path(source: Path, output: Path) -> bool:
    try:
        return source.resolve(strict=True) == output.resolve(strict=False)
    except OSError:
        return source.absolute() == output.absolute()


def _request_values(request: ShipmentOutput) -> dict[str, str]:
    return {
        "warehouse_code": request.warehouse_code,
        "warehouse_address": request.warehouse_address,
        "shipping_channel": request.channel,
        "delivery_date": request.delivery_date.isoformat(),
        "sailing_date": request.sailing_date.isoformat(),
        "delivery_warehouse": request.delivery_warehouse,
    }


def _generate_payload(source: Path, request: ShipmentOutput) -> _GeneratedPayload:
    if source.suffix.lower() != ".xlsx" or not source.is_file():
        raise WorkbookError("INVALID_PATH", "请选择存在的 .xlsx 工作簿文件")
    parsed = parse_invoice_bundle(source)
    target = next((item for item in parsed.shipments if item.shipment_id == request.shipment_id), None)
    if target is None:
        raise WorkbookError("SHIPMENT_NOT_FOUND", "未找到指定的货件", request.shipment_id)
    kept_rows = tuple(item.source_row for item in target.items)
    with SafePackage.open(source) as package:
        inventory = parse_workbook_drawings(source, parsed)
        relationships = _workbook_relationships(package)
        worksheet_part, _worksheet = _worksheet_part(package, relationships)
        worksheet_rel_part = _relationship_part(worksheet_part)
        sheet_xml = package.read(worksheet_part, MAX_ENTRY)
        workbook_xml = package.read("xl/workbook.xml", MAX_ENTRY)
        transformed: dict[str, bytes] = {
            worksheet_part: transform_sheet_xml(
                sheet_xml,
                parsed,
                kept_rows,
                _request_values(request),
                sheet_name=STANDARD_V1.sheet_name,
            ),
            "xl/workbook.xml": transform_workbook_xml(
                workbook_xml, parsed, kept_rows, sheet_name=STANDARD_V1.sheet_name
            ),
        }
        worksheet_relations = _relations(package, worksheet_rel_part)
        drawing_part = inventory.drawing_part
        removed_relationships: frozenset[str] = frozenset()
        removed_media: set[str] = set()
        if drawing_part is not None:
            drawing_xml = package.read(drawing_part, MAX_ENTRY)
            selected_ids = {
                image.relationship_id
                for image in inventory.product_images
                if image.source_row in set(kept_rows)
            }
            removed_relationships = frozenset(
                image.relationship_id
                for image in inventory.product_images
                if image.relationship_id not in selected_ids
            )
            transformed[drawing_part] = transform_drawing_anchors_xml(
                drawing_xml,
                parsed,
                kept_rows,
                removed_relationship_ids=removed_relationships,
            )
            drawing_rel_part = _relationship_part(drawing_part)
            drawing_rels = _relations(package, drawing_rel_part)
            transformed[drawing_rel_part] = _remove_relationships(drawing_rels, removed_relationships)
            removed_media = {
                inventory.relationships[relationship_id]
                for relationship_id in removed_relationships
                if relationship_id in inventory.relationships
            }
            transformed["[Content_Types].xml"] = _remove_content_type_overrides(
                package.read("[Content_Types].xml", MAX_ENTRY), removed_media
            )

        for relation in worksheet_relations:
            relation_type = (relation.get("Type") or "").lower()
            target_part = relation.get("Target")
            if not target_part:
                continue
            try:
                resolved = package.resolve_relationship_target(worksheet_part, target_part)
            except WorkbookError:
                continue
            if relation_type == COMMENTS_REL_TYPE.lower():
                refs = _comment_references(package, resolved)
                transformed[resolved] = transform_comments_xml(package.read(resolved, MAX_ENTRY), parsed, kept_rows)
                vml_relation = next(
                    (r for r in worksheet_relations if (r.get("Type") or "").lower() == VML_REL_TYPE.lower()), None
                )
                if vml_relation is None:
                    raise WorkbookError("UNSAFE_REFERENCE", "批注缺少对应 VML", worksheet_rel_part)
                vml_target = vml_relation.get("Target")
                if not vml_target:
                    raise WorkbookError("UNSAFE_REFERENCE", "批注 VML 关系无目标", worksheet_rel_part)
                vml_part = package.resolve_relationship_target(worksheet_part, vml_target)
                transformed[vml_part] = transform_comments_vml_xml(
                    package.read(vml_part, MAX_ENTRY), parsed, kept_rows, comment_references=refs
                )

        source_sheet_root = etree.fromstring(sheet_xml)
        source_workbook_root = etree.fromstring(workbook_xml)
        row_map = build_row_map(parsed.product_rows, kept_rows, parsed.total_row)
        shift = len(parsed.product_rows) - len(kept_rows)
        expected_row_attributes: dict[int, Mapping[str, str]] = {}
        for row in source_sheet_root.xpath("./main:sheetData/main:row", namespaces={"main": MAIN_NS}):
            old_number = int(row.get("r") or "0")
            new_number = row_map.get(old_number)
            if new_number is None:
                if old_number in parsed.product_rows:
                    continue
                new_number = old_number - shift if old_number > parsed.total_row else old_number
            expected_row_attributes[new_number] = {
                key: value for key, value in row.attrib.items() if key != "r"
            }
        transformed_lower = {name.lower() for name in transformed}
        removed_media_lower = {part.lower() for part in removed_media}
        unchanged = {
            name: hashlib.sha256(package.read(name, MAX_ENTRY)).hexdigest()
            for name in package.names
            if name.lower() not in transformed_lower and name.lower() not in removed_media_lower
        }
        target_image_count = sum(image.source_row in set(kept_rows) for image in inventory.product_images)
        payload = _write_deterministic_package(package, transformed, removed_media)
        return _GeneratedPayload(
            payload=payload,
            expected_rows=len(kept_rows),
            expected_images=target_image_count + len(inventory.fixed_images),
            expected_fixed_images=len(inventory.fixed_images),
            expected_merge_count=len(source_sheet_root.xpath("./main:mergeCells/main:mergeCell", namespaces={"main": MAIN_NS})),
            expected_print_area=bool(source_workbook_root.xpath("./main:definedNames/main:definedName[@name='_xlnm.Print_Area']", namespaces={"main": MAIN_NS})),
            expected_unchanged_parts=unchanged,
            expected_row_attributes=expected_row_attributes,
            expected_cols_xml=(
                cast(bytes, etree.tostring(source_sheet_root.find(f"{{{MAIN_NS}}}cols"), encoding="UTF-8"))
                if source_sheet_root.find(f"{{{MAIN_NS}}}cols") is not None
                else None
            ),
            expected_fixed_media=tuple(image.media_part for image in inventory.fixed_images),
            expected_fixed_anchors=tuple(
                (
                    image.anchor.from_row,
                    image.anchor.from_col,
                    image.anchor.from_row_off,
                    image.anchor.from_col_off,
                    image.anchor.to_row,
                    image.anchor.to_col,
                    image.anchor.to_row_off,
                    image.anchor.to_col_off,
                )
                for image in inventory.fixed_images
            ),
        )


def _validated_payload(generated: _GeneratedPayload, request: ShipmentOutput) -> bytes:
    with tempfile.TemporaryDirectory(prefix="fis-output-") as directory:
        path = Path(directory) / "generated.xlsx"
        path.write_bytes(generated.payload)
        report = validate_generated_workbook(
            path,
            expected_shipment=request.shipment_id,
            expected_rows=generated.expected_rows,
            expected_images=generated.expected_images,
            expected_fixed_images=generated.expected_fixed_images,
            expected_merge_count=generated.expected_merge_count,
            expected_print_area=generated.expected_print_area,
            expected_row_attributes=generated.expected_row_attributes,
            expected_cols_xml=generated.expected_cols_xml,
            expected_fixed_media=generated.expected_fixed_media,
            expected_fixed_anchors=generated.expected_fixed_anchors,
            expected_values=_request_values(request),
            expected_unchanged_parts=generated.expected_unchanged_parts,
        )
        if not report.ok:
            raise WorkbookError("OUTPUT_VALIDATION_FAILED", report.error or "生成工作簿校验失败")
    return generated.payload


def _relationship_part(source_part: str) -> str:
    path = PurePosixPath(source_part)
    return str(path.parent / "_rels" / f"{path.name}.rels")


def _relations(package: SafePackage, part: str) -> list[etree._Element]:
    try:
        root = package.read_xml(part, MAX_ENTRY)
    except WorkbookError as exc:
        if exc.code == "MISSING_PART":
            return []
        raise
    if root.tag != f"{{{PACKAGE_REL_NS}}}Relationships":
        raise WorkbookError("INVALID_RELATIONSHIP", "XLSX 关系部件无效", part)
    return list(root)


def _remove_relationships(relations: list[etree._Element], removed: frozenset[str]) -> bytes:
    root = etree.Element(f"{{{PACKAGE_REL_NS}}}Relationships", nsmap={None: PACKAGE_REL_NS})
    for relation in relations:
        if relation.get("Id") not in removed:
            root.append(etree.fromstring(etree.tostring(relation)))
    return cast(bytes, etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True))


def _remove_content_type_overrides(payload: bytes, removed_parts: set[str]) -> bytes:
    if not removed_parts:
        return payload
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(payload, parser=parser)
    for override in list(root):
        if override.tag != f"{{{CONTENT_TYPES_NS}}}Override":
            continue
        part_name = (override.get("PartName") or "").lstrip("/").lower()
        if part_name in {part.lower() for part in removed_parts}:
            root.remove(override)
    return cast(bytes, etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True))


def _comment_references(package: SafePackage, comments_part: str) -> tuple[str, ...]:
    root = package.read_xml(comments_part, MAX_ENTRY)
    comments = root.xpath("./main:commentList/main:comment", namespaces={"main": MAIN_NS})
    refs = tuple(comment.get("ref") or "" for comment in comments)
    if any(not ref for ref in refs):
        raise WorkbookError("UNSAFE_REFERENCE", "批注缺少单元格引用", comments_part)
    return refs


def _write_deterministic_package(
    package: SafePackage, transformed: Mapping[str, bytes], removed_media: set[str]
) -> bytes:
    removed_lower = {
        part.lower()
        for part in removed_media
        if part.lower() not in _retained_media(package, transformed)
    }
    from io import BytesIO

    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name in package.names:
            if name.lower() in removed_lower:
                continue
            payload = transformed.get(name)
            if payload is None:
                payload = transformed.get(name.lower())
            if payload is None:
                payload = package.read(name, MAX_ENTRY)
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload)
    return output.getvalue()


def _retained_media(package: SafePackage, transformed: Mapping[str, bytes]) -> set[str]:
    """Find media still reachable after transformed drawing relationship parts."""

    retained: set[str] = set()
    for name in package.names:
        if not name.lower().endswith(".rels"):
            continue
        payload = transformed.get(name, package.read(name, MAX_ENTRY))
        try:
            root = etree.fromstring(payload)
        except etree.XMLSyntaxError:
            continue
        for relation in root:
            if (relation.get("Type") or "").lower() != IMAGE_REL_TYPE.lower():
                continue
            target = relation.get("Target")
            if not target:
                continue
            try:
                retained.add(package.resolve_relationship_part_target(name, target).lower())
            except WorkbookError:
                continue
    return retained


# Imported lazily at module end to avoid the validator importing this module.
from fis.xlsx.validate import validate_generated_workbook
