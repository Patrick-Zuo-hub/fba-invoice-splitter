"""Build and publish one workbook containing every Shipment worksheet."""

from __future__ import annotations

import os
import posixpath
import re
import tempfile
from collections.abc import Sequence
from importlib import resources
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from lxml import etree  # type: ignore[import-untyped]

from fis.xlsx.errors import WorkbookError
from fis.xlsx.generator import (
    ShipmentOutput,
    generate_shipment_workbook_bytes,
    request_values,
)
from fis.xlsx.package import MAX_ENTRY, SafePackage
from fis.xlsx.template import STANDARD_V1
from fis.xlsx.validate import validate_combined_workbook

_FORBIDDEN_SHEET_CHARACTERS = re.compile(r"[\[\]:*?/\\]")
_MAX_SHEET_TITLE = 31

REFERENCE_SHEET_TITLE = "渠道分解&Fba编码"
"""Title of the reference worksheet appended after every Shipment worksheet."""

_REFERENCE_ASSET = "渠道分解&Fba编码.xlsx"


def combined_filename(source_filename: str) -> str:
    """Return the v1.1 output name derived from the user-visible upload name."""

    display = source_filename.replace("\\", "/").split("/")[-1]
    stem = Path(display).stem
    stem = "".join(character for character in stem if character.isprintable()).strip(
        " .'_"
    )
    if not stem:
        stem = "发票"
    return f"已处理_{stem}.xlsx"


def next_output_path(output_dir: Path, source_filename: str) -> Path:
    """Return the first available output path without overwriting case variants."""

    first = combined_filename(source_filename)
    occupied = {child.name.casefold() for child in output_dir.iterdir()}
    if first.casefold() not in occupied:
        return output_dir / first
    stem = Path(first).stem
    index = 2
    while f"{stem}_{index}.xlsx".casefold() in occupied:
        index += 1
    return output_dir / f"{stem}_{index}.xlsx"


def worksheet_titles(requests: Sequence[ShipmentOutput]) -> tuple[str, ...]:
    """Allocate legal, deterministic and case-insensitively unique Sheet titles."""

    occupied: set[str] = {REFERENCE_SHEET_TITLE.casefold()}
    result: list[str] = []
    for request in requests:
        preferred = f"{request.warehouse_code}-{request.shipment_id}"
        base = _FORBIDDEN_SHEET_CHARACTERS.sub("_", preferred).strip().strip("'")
        base = base or "货件"
        base = base[:_MAX_SHEET_TITLE]
        candidate = base
        index = 2
        while candidate.casefold() in occupied:
            suffix = f"_{index}"
            candidate = f"{base[: _MAX_SHEET_TITLE - len(suffix)]}{suffix}"
            index += 1
        occupied.add(candidate.casefold())
        result.append(candidate)
    return tuple(result)


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

_CONTENT_TYPES_PART = "[Content_Types].xml"
_WORKBOOK_PART = "xl/workbook.xml"
_SHARED_STRINGS_PART = "xl/sharedStrings.xml"
_DROPPED_PARTS = frozenset({"xl/calcChain.xml"})
_XML_DECLARATION = b"<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n"

Parts = dict[str, bytes]


def _combined_error(code: str, message: str, location: str | None = None) -> WorkbookError:
    return WorkbookError(code, message, location)


def _read_parts(payload: bytes) -> Parts:
    try:
        with ZipFile(BytesIO(payload)) as archive:
            return {name: archive.read(name) for name in archive.namelist()}
    except Exception as error:
        raise _combined_error("INVALID_COMBINED_INPUT", "合并工作簿输入无效") from error


def _parse(parts: Parts, part: str) -> etree._Element:
    payload = parts.get(part)
    if payload is None:
        raise _combined_error("COMBINED_RELATIONSHIP_INVALID", "合并工作簿缺少部件", part)
    try:
        return etree.fromstring(payload)
    except etree.XMLSyntaxError as error:
        raise _combined_error("COMBINED_RELATIONSHIP_INVALID", "合并工作簿部件无效", part) from error


def _serialize(root: etree._Element) -> bytes:
    payload: bytes = etree.tostring(root, xml_declaration=False, encoding="UTF-8")
    return _XML_DECLARATION + payload


def _rels_part(part: str) -> str:
    directory, name = posixpath.split(part)
    return posixpath.join(directory, "_rels", f"{name}.rels")


def _resolve(base_part: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))


def _relative(target_part: str, base_part: str) -> str:
    return posixpath.relpath(target_part, posixpath.dirname(base_part) or ".")


def _internal_relationships(parts: Parts, part: str) -> list[etree._Element]:
    rels = parts.get(_rels_part(part))
    if rels is None:
        return []
    root = etree.fromstring(rels)
    return [
        relation
        for relation in root.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
        if (relation.get("TargetMode") or "Internal") != "External"
    ]


def _closure(parts: Parts, part: str) -> set[str]:
    """Return every internal part reachable from ``part`` (excluding itself)."""

    seen: set[str] = set()
    pending = [part]
    while pending:
        current = pending.pop()
        rels = _rels_part(current)
        if rels in parts and rels not in seen:
            seen.add(rels)
        for relation in _internal_relationships(parts, current):
            target = _resolve(current, relation.get("Target") or "")
            if target in seen or target == part:
                continue
            seen.add(target)
            pending.append(target)
    return seen


def _selected_worksheet(parts: Parts, sheet_name: str | None = STANDARD_V1.sheet_name) -> str:
    """Resolve one worksheet part by title, or the first worksheet when None."""

    workbook = _parse(parts, _WORKBOOK_PART)
    rels = _parse(parts, _rels_part(_WORKBOOK_PART))
    targets = {
        relation.get("Id"): relation.get("Target") or ""
        for relation in rels.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
    }
    for sheet in workbook.iterfind(f"{{{MAIN_NS}}}sheets/{{{MAIN_NS}}}sheet"):
        if sheet_name is not None and sheet.get("name") != sheet_name:
            continue
        target = targets.get(sheet.get(f"{{{OFFICE_REL_NS}}}id") or "")
        if target is None:
            break
        return _resolve(_WORKBOOK_PART, target)
    raise _combined_error(
        "INVALID_COMBINED_INPUT", "货件工作簿缺少标准工作表", sheet_name or _WORKBOOK_PART
    )


def _content_type_of(parts: Parts, part: str) -> str | None:
    root = _parse(parts, _CONTENT_TYPES_PART)
    for override in root.findall(f"{{{CONTENT_TYPES_NS}}}Override"):
        if (override.get("PartName") or "").lstrip("/") == part:
            content_type = override.get("ContentType")
            return None if content_type is None else str(content_type)
    return None


def _merge_shared_strings(packages: Sequence[Parts]) -> tuple[bytes | None, list[dict[int, int]]]:
    """Merge every package's shared strings and map old indices to merged ones."""

    if not any(_SHARED_STRINGS_PART in package for package in packages):
        return None, [{} for _ in packages]

    merged: list[etree._Element] = []
    positions: dict[bytes, int] = {}
    mappings: list[dict[int, int]] = []
    for package in packages:
        mapping: dict[int, int] = {}
        payload = package.get(_SHARED_STRINGS_PART)
        if payload is not None:
            table = etree.fromstring(payload)
            for index, item in enumerate(table.findall(f"{{{MAIN_NS}}}si")):
                key = etree.tostring(item)
                position = positions.get(key)
                if position is None:
                    position = len(merged)
                    positions[key] = position
                    merged.append(item)
                mapping[index] = position
        mappings.append(mapping)

    table = etree.Element(f"{{{MAIN_NS}}}sst", nsmap={None: MAIN_NS})
    table.set("count", str(len(merged)))
    table.set("uniqueCount", str(len(merged)))
    for item in merged:
        table.append(item)
    return _serialize(table), mappings


def _remap_shared_string_indices(payload: bytes, mapping: dict[int, int]) -> bytes:
    if not mapping:
        return payload
    root = etree.fromstring(payload)
    changed = False
    for cell in root.iter(f"{{{MAIN_NS}}}c"):
        if cell.get("t") != "s":
            continue
        value = cell.find(f"{{{MAIN_NS}}}v")
        if value is None or not value.text:
            continue
        try:
            old = int(value.text)
        except ValueError as error:
            raise _combined_error(
                "COMBINED_RELATIONSHIP_INVALID", "共享字符串索引无效", cell.get("r")
            ) from error
        new = mapping.get(old)
        if new is None:
            raise _combined_error(
                "COMBINED_RELATIONSHIP_INVALID", "共享字符串索引越界", cell.get("r")
            )
        if new != old:
            value.text = str(new)
            changed = True
    return _serialize(root) if changed else payload


def _allocate_name(used: set[str], old_part: str, index: int) -> str:
    directory, name = posixpath.split(old_part)
    stem, extension = posixpath.splitext(name)
    base = re.sub(r"\d+$", "", stem) or stem
    candidate = posixpath.join(directory, f"{base}{index}{extension}")
    attempt = 1
    while candidate in used:
        attempt += 1
        candidate = posixpath.join(directory, f"{base}{index}_{attempt}{extension}")
    used.add(candidate)
    return candidate


def _clone_sheet_graph(
    assembled: Parts,
    used: set[str],
    package: Parts,
    worksheet_part: str,
    index: int,
    shared_mapping: dict[int, int],
    overrides: dict[str, str],
) -> str:
    """Copy one worksheet and its private parts under collision-free names."""

    closure = _closure(package, worksheet_part)
    owned = [part for part in sorted(closure) if not part.endswith(".rels")]
    renames = {worksheet_part: _allocate_name(used, worksheet_part, index)}
    for part in owned:
        renames[part] = _allocate_name(used, part, index)

    for old_part, new_part in renames.items():
        payload = package.get(old_part)
        if payload is None:
            raise _combined_error("COMBINED_RELATIONSHIP_INVALID", "货件包缺少部件", old_part)
        if old_part == worksheet_part:
            payload = _remap_shared_string_indices(payload, shared_mapping)
        assembled[new_part] = payload
        content_type = _content_type_of(package, old_part)
        if content_type is not None:
            overrides[new_part] = content_type

        old_rels = _rels_part(old_part)
        if old_rels not in package:
            continue
        rels_root = etree.fromstring(package[old_rels])
        for relation in rels_root.findall(f"{{{PACKAGE_REL_NS}}}Relationship"):
            if (relation.get("TargetMode") or "Internal") == "External":
                continue
            target = _resolve(old_part, relation.get("Target") or "")
            mapped = renames.get(target)
            if mapped is None:
                raise _combined_error(
                    "COMBINED_RELATIONSHIP_INVALID", "货件关系目标无法克隆", target
                )
            relation.set("Target", _relative(mapped, new_part))
        assembled[_rels_part(new_part)] = _serialize(rels_root)

    return renames[worksheet_part]


def _load_reference_package() -> tuple[Parts, str]:
    """Load the bundled reference workbook and return its parts and worksheet.

    The reference worksheet carries its own ``styles.xml``, whose indices mean
    something different in the assembled workbook. It declares no custom number
    formats, so dropping the cell style indices loses only fonts, fills and
    borders while every value — and the column widths — survive intact.
    """

    asset = resources.files("fis.xlsx.assets").joinpath(_REFERENCE_ASSET)
    try:
        payload = asset.read_bytes()
    except (OSError, ModuleNotFoundError) as error:
        raise _combined_error(
            "REFERENCE_SHEET_MISSING", "缺少随附的渠道分解参考页", _REFERENCE_ASSET
        ) from error

    package = _read_parts(payload)
    worksheet_part = _selected_worksheet(package, sheet_name=None)
    worksheet = _parse(package, worksheet_part)
    for cell in worksheet.iter(f"{{{MAIN_NS}}}c"):
        cell.attrib.pop("s", None)
    package[worksheet_part] = _serialize(worksheet)
    return package, worksheet_part


def _print_area_reference(workbook: etree._Element) -> str | None:
    for name in workbook.iterfind(f"{{{MAIN_NS}}}definedNames/{{{MAIN_NS}}}definedName"):
        if name.get("name") == "_xlnm.Print_Area" and name.text and "!" in name.text:
            return str(name.text.split("!", 1)[1])
    return None


def _rebuild_workbook(
    assembled: Parts,
    base_workbook: etree._Element,
    titles: Sequence[str],
    print_areas: Sequence[str | None],
) -> None:
    sheets = base_workbook.find(f"{{{MAIN_NS}}}sheets")
    if sheets is None:
        raise _combined_error("INVALID_COMBINED_INPUT", "工作簿缺少 sheets 节点", _WORKBOOK_PART)
    for child in list(sheets):
        sheets.remove(child)
    for position, title in enumerate(titles, start=1):
        sheet = etree.SubElement(sheets, f"{{{MAIN_NS}}}sheet")
        sheet.set("name", title)
        sheet.set("sheetId", str(position))
        sheet.set(f"{{{OFFICE_REL_NS}}}id", f"rIdSheet{position}")

    existing = base_workbook.find(f"{{{MAIN_NS}}}definedNames")
    if existing is not None:
        base_workbook.remove(existing)
    if any(print_area is not None for print_area in print_areas):
        defined_names = etree.Element(f"{{{MAIN_NS}}}definedNames")
        for position, (title, print_area) in enumerate(zip(titles, print_areas, strict=True)):
            if print_area is None:
                continue
            entry = etree.SubElement(defined_names, f"{{{MAIN_NS}}}definedName")
            entry.set("name", "_xlnm.Print_Area")
            entry.set("localSheetId", str(position))
            entry.text = f"'{title}'!{print_area}"
        sheets.addnext(defined_names)
    assembled[_WORKBOOK_PART] = _serialize(base_workbook)


def _rebuild_workbook_relationships(
    assembled: Parts, base_rels: etree._Element, worksheets: Sequence[str], shared: bool
) -> None:
    for relation in list(base_rels):
        relation_type = relation.get("Type") or ""
        target = _resolve(_WORKBOOK_PART, relation.get("Target") or "")
        if relation_type.endswith("/worksheet") or target not in assembled:
            base_rels.remove(relation)
    if shared and not any(
        (relation.get("Type") or "").endswith("/sharedStrings") for relation in base_rels
    ):
        relation = etree.SubElement(base_rels, f"{{{PACKAGE_REL_NS}}}Relationship")
        relation.set("Id", "rIdSharedStrings")
        relation.set(
            "Type",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings",
        )
        relation.set("Target", "sharedStrings.xml")
    for position, worksheet_part in enumerate(worksheets, start=1):
        relation = etree.SubElement(base_rels, f"{{{PACKAGE_REL_NS}}}Relationship")
        relation.set("Id", f"rIdSheet{position}")
        relation.set(
            "Type",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
        )
        relation.set("Target", _relative(worksheet_part, _WORKBOOK_PART))
    assembled[_rels_part(_WORKBOOK_PART)] = _serialize(base_rels)


def _sync_content_types(assembled: Parts, base: etree._Element, overrides: dict[str, str]) -> None:
    defaults = [child for child in base if child.tag == f"{{{CONTENT_TYPES_NS}}}Default"]
    known = {(default.get("Extension") or "").casefold() for default in defaults}
    root = etree.Element(f"{{{CONTENT_TYPES_NS}}}Types", nsmap={None: CONTENT_TYPES_NS})
    for default in defaults:
        root.append(default)
    for part in assembled:
        extension = posixpath.splitext(part)[1].lstrip(".").casefold()
        if extension and extension not in known:
            known.add(extension)
            default = etree.SubElement(root, f"{{{CONTENT_TYPES_NS}}}Default")
            default.set("Extension", extension)
            default.set("ContentType", "application/octet-stream")
    kept: dict[str, str] = {}
    for override in base.findall(f"{{{CONTENT_TYPES_NS}}}Override"):
        part = (override.get("PartName") or "").lstrip("/")
        content_type = override.get("ContentType")
        if part in assembled and content_type is not None:
            kept[part] = content_type
    kept.update(overrides)
    for part in sorted(kept):
        override = etree.SubElement(root, f"{{{CONTENT_TYPES_NS}}}Override")
        override.set("PartName", f"/{part}")
        override.set("ContentType", kept[part])
    assembled[_CONTENT_TYPES_PART] = _serialize(root)


def _assert_no_missing_internal_targets(assembled: Parts) -> None:
    for part in list(assembled):
        if not part.endswith(".rels"):
            continue
        directory = posixpath.dirname(posixpath.dirname(part))
        owner = posixpath.join(directory, posixpath.basename(part)[: -len(".rels")])
        root = etree.fromstring(assembled[part])
        for relation in root.findall(f"{{{PACKAGE_REL_NS}}}Relationship"):
            if (relation.get("TargetMode") or "Internal") == "External":
                continue
            target = _resolve(owner, relation.get("Target") or "")
            if target not in assembled:
                raise _combined_error(
                    "COMBINED_RELATIONSHIP_INVALID", "合并工作簿关系目标缺失", target
                )


def _write_package(parts: Parts) -> bytes:
    ordered = [_CONTENT_TYPES_PART] + sorted(
        name for name in parts if name != _CONTENT_TYPES_PART
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name in ordered:
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            archive.writestr(info, parts[name])
    return buffer.getvalue()


def assemble_combined_workbook(payloads: Sequence[bytes], titles: Sequence[str]) -> bytes:
    """Build one workbook exposing every Shipment worksheet in the given order.

    The bundled reference worksheet is always appended after the last Shipment,
    because the downstream logistics system reads it from the final position.
    """

    if not payloads or len(payloads) != len(titles):
        raise _combined_error("INVALID_COMBINED_INPUT", "合并工作簿输入无效")

    packages = [_read_parts(payload) for payload in payloads]
    worksheets = [_selected_worksheet(package) for package in packages]

    reference_package, reference_worksheet = _load_reference_package()
    packages.append(reference_package)
    worksheets.append(reference_worksheet)
    titles = [*titles, REFERENCE_SHEET_TITLE]

    shared_table, mappings = _merge_shared_strings(packages)

    base = packages[0]
    private = _closure(base, worksheets[0]) | {worksheets[0], _rels_part(worksheets[0])}
    assembled: Parts = {
        name: payload
        for name, payload in base.items()
        if name not in private
        and name not in _DROPPED_PARTS
        and not name.startswith("xl/worksheets/")
        and name != _SHARED_STRINGS_PART
    }
    if shared_table is not None:
        assembled[_SHARED_STRINGS_PART] = shared_table

    used: set[str] = set(assembled)
    overrides: dict[str, str] = {}
    cloned = [
        _clone_sheet_graph(
            assembled, used, package, worksheet, index + 1, mappings[index], overrides
        )
        for index, (package, worksheet) in enumerate(zip(packages, worksheets, strict=True))
    ]

    print_areas = [
        _print_area_reference(_parse(package, _WORKBOOK_PART)) for package in packages
    ]
    _rebuild_workbook(assembled, _parse(base, _WORKBOOK_PART), titles, print_areas)
    _rebuild_workbook_relationships(
        assembled, _parse(base, _rels_part(_WORKBOOK_PART)), cloned, shared_table is not None
    )
    _sync_content_types(assembled, _parse(base, _CONTENT_TYPES_PART), overrides)
    _assert_no_missing_internal_targets(assembled)
    return _write_package(assembled)


def combined_sheet_names(path: Path | str) -> tuple[str, ...]:
    """Return the visible worksheet titles of a combined workbook, in order."""

    with SafePackage.open(path) as package:
        workbook = package.read_xml(_WORKBOOK_PART, MAX_ENTRY)
        sheets = workbook.xpath("./main:sheets/main:sheet", namespaces={"main": MAIN_NS})
        return tuple(str(sheet.get("name") or "") for sheet in sheets)


def _validate_and_publish(
    payload: bytes,
    output_path: Path,
    requests: Sequence[ShipmentOutput],
    titles: Sequence[str],
) -> Path:
    """Write, validate and only then atomically publish the combined workbook."""

    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed by the with block below
        dir=output_path.parent, prefix=".fis-combined-", suffix=".xlsx", delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        report = validate_combined_workbook(
            temporary,
            titles=tuple(titles),
            expectations=tuple((request.shipment_id, request_values(request)) for request in requests),
            reference_title=REFERENCE_SHEET_TITLE,
        )
        if not report.ok:
            raise _combined_error(
                "COMBINED_VALIDATION_FAILED", report.error or "合并工作簿校验失败", report.location
            )
        os.replace(temporary, output_path)
        return output_path
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def generate_combined_workbook(
    source: Path | str,
    requests: Sequence[ShipmentOutput],
    output_dir: Path | str,
    source_filename: str,
) -> Path:
    """Generate every Shipment and publish them as one validated workbook."""

    if not requests:
        raise _combined_error("INVALID_COMBINED_INPUT", "没有可生成的货件")
    destination = Path(output_dir)
    if not destination.is_dir():
        raise _combined_error("OUTPUT_PATH_INVALID", "输出目录路径不是目录", str(destination))
    titles = worksheet_titles(requests)
    payloads = tuple(generate_shipment_workbook_bytes(source, request) for request in requests)
    payload = assemble_combined_workbook(payloads, titles)
    return _validate_and_publish(payload, next_output_path(destination, source_filename), requests, titles)


__all__ = [
    "assemble_combined_workbook",
    "combined_filename",
    "combined_sheet_names",
    "generate_combined_workbook",
    "next_output_path",
    "worksheet_titles",
]
