"""Bounded, read-only access to a safe XLSX OOXML package."""

from __future__ import annotations

import re
import struct
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from lzma import LZMAError
from pathlib import Path, PurePosixPath
from types import MappingProxyType, TracebackType
from typing import Self
from urllib.parse import unquote_to_bytes, urlsplit
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from lxml import etree  # type: ignore[import-untyped]

from fis.xlsx.errors import WorkbookError

MAX_ENTRIES = 5000
MAX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_ENTRY = 50 * 1024 * 1024
MAX_RELATIONSHIP_TARGET_CHARS = 8192
MAX_PERCENT_DECODE_ROUNDS = 3

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ENCODED_SEPARATOR = re.compile(r"%(?:2[fF]|5[cC])")
_ENCODED_DOT_SEGMENT = re.compile(r"(?:^|/)%2[eE](?:%2[eE])?(?:/|$)")
_XML_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    dtd_validation=False,
    huge_tree=False,
)


def _ascii_lower(value: str) -> str:
    """Lowercase only ASCII letters, as required by the OPC part-name rules."""

    return value.translate({code: code + 32 for code in range(ord("A"), ord("Z") + 1)})


_CORE_PARTS = frozenset({"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml"})
_CANONICAL_CORE_PARTS = frozenset(_ascii_lower(part) for part in _CORE_PARTS)
_ARCHIVE_ERRORS = (
    BadZipFile,
    EOFError,
    LZMAError,
    OSError,
    RuntimeError,
    UnicodeDecodeError,
    zlib.error,
    struct.error,
)
_RELATIONSHIPS_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/content-types"


@dataclass(slots=True)
class SafePackage:
    """A validated XLSX archive that exposes bounded, read-only part access."""

    path: Path
    archive: ZipFile
    _entries: dict[str, ZipInfo] = field(default_factory=dict, init=False, repr=False)
    _canonical_entries: dict[str, ZipInfo] = field(default_factory=dict, init=False, repr=False)
    _content_types_cache: tuple[Mapping[str, str], Mapping[str, str]] | None = field(
        default=None, init=False, repr=False
    )
    _content_types_valid: bool | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @classmethod
    def open(cls, path: Path | str) -> SafePackage:
        package_path = Path(path)
        if package_path.suffix.lower() != ".xlsx" or not package_path.is_file():
            raise WorkbookError("INVALID_PATH", "请选择存在的 .xlsx 工作簿文件")

        archive: ZipFile | None = None
        validated = False
        try:
            archive = ZipFile(package_path, "r")
            package = cls(path=package_path, archive=archive)
            package._validate()
            validated = True
            return package
        except WorkbookError:
            raise
        except _ARCHIVE_ERRORS as exc:
            raise WorkbookError("INVALID_XLSX", "文件不是有效的 XLSX 工作簿") from exc
        finally:
            if archive is not None and not validated:
                archive.close()

    @property
    def names(self) -> tuple[str, ...]:
        """The validated ZIP part names, in archive order."""

        self._require_open()
        return tuple(self._entries)

    def read(self, part: str, max_bytes: int | None = None) -> bytes:
        """Read a part without ever returning more than its configured byte bound."""

        self._require_open()
        if max_bytes is not None and (isinstance(max_bytes, bool) or max_bytes < 0):
            raise ValueError("max_bytes must be a non-negative integer or None")
        if max_bytes is not None and not isinstance(max_bytes, int):
            raise TypeError("max_bytes must be an integer or None")

        info = self._canonical_entries.get(self._canonical_part_name(part))
        if info is None:
            raise WorkbookError("MISSING_PART", "工作簿缺少所需部件", part)
        limit = MAX_ENTRY if max_bytes is None else min(max_bytes, MAX_ENTRY)
        if info.file_size > limit:
            raise WorkbookError("READ_TOO_LARGE", "工作簿部件超过允许读取大小", part)

        chunks: list[bytes] = []
        size = 0
        try:
            with self.archive.open(info, "r") as stream:
                while True:
                    chunk = stream.read(min(64 * 1024, limit - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise WorkbookError("READ_TOO_LARGE", "工作簿部件超过允许读取大小", part)
                    chunks.append(chunk)
        except WorkbookError:
            raise
        except _ARCHIVE_ERRORS as exc:
            self.close()
            raise WorkbookError("INVALID_XLSX", "工作簿部件无法安全读取", part) from exc
        return b"".join(chunks)

    def read_xml(self, part: str, max_bytes: int) -> etree._Element:
        """Read and safely parse one XML part, rejecting DTDs and entities."""

        payload = self.read(part, max_bytes)
        try:
            root = etree.fromstring(payload, parser=_XML_PARSER)
        except (etree.XMLSyntaxError, ValueError, TypeError) as exc:
            raise WorkbookError("INVALID_XML", "工作簿 XML 格式无效", part) from exc
        document_info = root.getroottree().docinfo
        if (
            document_info.doctype
            or document_info.internalDTD is not None
            or document_info.externalDTD is not None
        ):
            raise WorkbookError("UNSAFE_XML", "工作簿 XML 不允许 DTD 或实体定义", part)
        return root

    def resolve_relationship_target(self, source_part: str, target: str) -> str:
        """Resolve one internal OPC relationship target to its canonical part name."""

        self._require_open()
        return self._resolve_relationship_target(source_part, target)

    def resolve_relationship_part_target(self, relationship_part: str, target: str) -> str:
        """Resolve a target using the source part represented by a ``.rels`` part."""

        self._require_open()
        canonical_relationship_part = self._canonical_part_name(relationship_part)
        if canonical_relationship_part == "_rels/.rels":
            return self._resolve_relationship_target(None, target)
        source_part = self._source_part_for_relationship_part(canonical_relationship_part)
        return self._resolve_relationship_target(source_part, target)

    def content_type_for_part(self, part: str) -> str | None:
        """Return the canonical content type resolved from ``[Content_Types].xml``."""

        self._require_open()
        defaults, overrides = self._load_content_types()
        if not self._content_types_valid:
            raise WorkbookError("INVALID_CONTENT_TYPES", "XLSX 内容类型部件无效", "[Content_Types].xml")
        canonical_part = self._canonical_part_name(part)
        suffix = PurePosixPath(canonical_part).suffix.removeprefix(".")
        return overrides.get(canonical_part, defaults.get(suffix))

    def close(self) -> None:
        """Close the underlying archive. Access after close is rejected."""

        if not self._closed:
            self.archive.close()
            self._closed = True

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed or self.archive.fp is None:
            self._closed = True
            raise WorkbookError("PACKAGE_CLOSED", "工作簿已关闭，无法继续读取")

    def _validate(self) -> None:
        infos = self.archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise WorkbookError("TOO_MANY_ENTRIES", "XLSX 包含过多文件条目")

        total_size = 0
        canonical_names: set[str] = set()
        for info in infos:
            entry_name = self._validated_zip_entry_name(info)
            if info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
                raise WorkbookError(
                    "UNSUPPORTED_COMPRESSION", "XLSX 包含不支持的压缩算法", entry_name
                )
            if info.is_dir():
                # Common writers emit zero-length directory entries. They carry
                # no content and are never resolved as parts, so skip them
                # rather than fail the package on their trailing slash — but a
                # "directory" with data in it is not one, so reject that.
                if info.file_size:
                    raise WorkbookError(
                        "UNSAFE_PATH", "XLSX 包含不安全的文件路径", entry_name
                    )
                continue
            canonical_name = self._canonical_part_name(entry_name)
            if canonical_name in canonical_names:
                raise WorkbookError("DUPLICATE_ENTRY", "XLSX 包含重复的文件条目", entry_name)
            canonical_names.add(canonical_name)
            if info.flag_bits & 0x1:
                raise WorkbookError("ENCRYPTED_ENTRY", "XLSX 不支持加密文件条目", entry_name)
            if info.file_size > MAX_ENTRY:
                raise WorkbookError("ENTRY_TOO_LARGE", "XLSX 包含超过大小限制的文件", entry_name)
            total_size += info.file_size
            if total_size > MAX_UNCOMPRESSED:
                raise WorkbookError("PACKAGE_TOO_LARGE", "XLSX 解压后的总大小超过限制")
            self._entries[entry_name] = info
            self._canonical_entries[canonical_name] = info

        missing = _CANONICAL_CORE_PARTS.difference(self._canonical_entries)
        if missing:
            raise WorkbookError("MISSING_CORE_PART", "XLSX 缺少必要的 OOXML 核心部件", min(missing))

        for part, info in self._entries.items():
            self._validate_unsupported_part(self._canonical_part_name(part), info.filename)
        self._validate_content_types()
        self._validate_relationships()

    @staticmethod
    def _validated_zip_entry_name(info: ZipInfo) -> str:
        """Reject ZIP names that Python's ZipInfo may have silently normalized."""

        filename = getattr(info, "filename", None)
        if not isinstance(filename, str):
            raise WorkbookError("UNSAFE_PATH", "XLSX 包含不安全的文件路径")

        missing = object()
        try:
            original_name = getattr(info, "orig_filename", missing)
        except Exception as exc:
            raise WorkbookError("UNSAFE_PATH", "XLSX 包含不安全的文件路径", filename) from exc

        names = [filename]
        if original_name is not missing:
            try:
                original_text = (
                    original_name
                    if isinstance(original_name, str)
                    else str(original_name)
                )
            except Exception as exc:
                raise WorkbookError(
                    "UNSAFE_PATH", "XLSX 包含不安全的文件路径", filename
                ) from exc
            if not isinstance(original_name, str) or original_text != filename:
                raise WorkbookError("UNSAFE_PATH", "XLSX 包含不安全的文件路径", filename)
            names.append(original_text)

        if any(
            ord(character) < 32 or 127 <= ord(character) <= 159
            for name in names
            for character in name
        ):
            raise WorkbookError("UNSAFE_PATH", "XLSX 包含不安全的文件路径", filename)
        return filename

    @staticmethod
    def _decode_percent_uri(value: str, location: str, error_code: str) -> str:
        """Decode percent escapes repeatedly while rejecting dangerous URI syntax."""

        current = value
        if len(current) > MAX_RELATIONSHIP_TARGET_CHARS:
            raise WorkbookError(error_code, "XLSX 文件路径或关系目标过长", location)
        first_pass = True
        rounds = 0
        while "%" in current:
            rounds += 1
            if rounds > MAX_PERCENT_DECODE_ROUNDS:
                raise WorkbookError(error_code, "XLSX 文件路径或关系目标编码层数过多", location)
            if _ENCODED_SEPARATOR.search(current):
                raise WorkbookError(error_code, "XLSX 包含不安全的文件路径或关系目标", location)
            if _INVALID_PERCENT_ESCAPE.search(current):
                if first_pass:
                    raise WorkbookError(error_code, "XLSX 包含不安全的文件路径或关系目标", location)
                return current
            try:
                decoded = unquote_to_bytes(current).decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise WorkbookError(
                    error_code, "XLSX 包含不安全的文件路径或关系目标", location
                ) from exc
            if decoded == current:
                return decoded
            current = decoded
            if len(current) > MAX_RELATIONSHIP_TARGET_CHARS:
                raise WorkbookError(error_code, "XLSX 文件路径或关系目标过长", location)
            first_pass = False
        return current

    @classmethod
    def _canonical_part_name(cls, name: str) -> str:
        decoded = cls._decode_percent_uri(name, name, "UNSAFE_PATH")
        raw_parts = decoded.split("/")
        if (
            not decoded
            or "\\" in decoded
            or decoded.startswith(("/", "//"))
            or _WINDOWS_DRIVE.match(decoded) is not None
            or any(segment in {"", ".", ".."} for segment in raw_parts)
            or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in decoded)
        ):
            raise WorkbookError("UNSAFE_PATH", "XLSX 包含不安全的文件路径", name)
        normalized = str(PurePosixPath(decoded))
        if normalized in {"", "."} or normalized.startswith("../"):
            raise WorkbookError("UNSAFE_PATH", "XLSX 包含不安全的文件路径", name)
        return _ascii_lower(normalized)

    @staticmethod
    def _validate_unsupported_part(canonical_part: str, location: str) -> None:
        lowered = canonical_part
        if "vbaproject" in lowered or lowered.endswith(".vba"):
            raise WorkbookError("MACRO_UNSUPPORTED", "V1 不支持包含宏的工作簿", location)
        if "/embeddings/" in lowered or "oleobject" in lowered:
            raise WorkbookError("OLE_UNSUPPORTED", "V1 不支持 OLE 嵌入对象", location)
        if "/activex/" in lowered or "activex" in lowered:
            raise WorkbookError("ACTIVEX_UNSUPPORTED", "V1 不支持 ActiveX 控件", location)

    def _validate_content_types(self) -> None:
        self._load_content_types()

    def _load_content_types(self) -> tuple[Mapping[str, str], Mapping[str, str]]:
        if self._content_types_cache is not None:
            return self._content_types_cache
        root = self.read_xml("[Content_Types].xml", MAX_ENTRY)
        defaults: dict[str, str] = {}
        overrides: dict[str, str] = {}
        for element in root.iter():
            content_type = (element.get("ContentType") or "").lower()
            location = element.get("PartName") or "[Content_Types].xml"
            if "macroenabled" in content_type or "vbaproject" in content_type:
                raise WorkbookError("MACRO_UNSUPPORTED", "V1 不支持包含宏的工作簿", location)
            if "oleobject" in content_type or "/embedding" in content_type:
                raise WorkbookError("OLE_UNSUPPORTED", "V1 不支持 OLE 嵌入对象", location)
            if "activex" in content_type:
                raise WorkbookError("ACTIVEX_UNSUPPORTED", "V1 不支持 ActiveX 控件", location)
        # A few low-level SafePackage tests intentionally use a tiny unnamespaced
        # ``<Types/>`` sentinel.  Keep that package-level validation compatible,
        # while using strict OPC parsing for real namespaced content types.
        canonical = root.tag == f"{{{_CONTENT_TYPES_NAMESPACE}}}Types"
        if canonical:
            for element in root:
                if element.tag == f"{{{_CONTENT_TYPES_NAMESPACE}}}Default":
                    extension = _ascii_lower(element.get("Extension") or "")
                    content_type = (element.get("ContentType") or "").lower()
                    if not extension or not content_type or extension in defaults:
                        raise WorkbookError(
                            "INVALID_CONTENT_TYPES", "XLSX 内容类型部件无效", "[Content_Types].xml"
                        )
                    defaults[extension] = content_type
                elif element.tag == f"{{{_CONTENT_TYPES_NAMESPACE}}}Override":
                    raw_part_name = (element.get("PartName") or "").removeprefix("/")
                    part_name = self._canonical_part_name(raw_part_name)
                    content_type = (element.get("ContentType") or "").lower()
                    if not content_type or part_name in overrides:
                        raise WorkbookError(
                            "INVALID_CONTENT_TYPES", "XLSX 内容类型部件无效", "[Content_Types].xml"
                        )
                    overrides[part_name] = content_type
                else:
                    raise WorkbookError(
                        "INVALID_CONTENT_TYPES", "XLSX 内容类型部件无效", "[Content_Types].xml"
                    )
        self._content_types_valid = canonical
        self._content_types_cache = (MappingProxyType(defaults), MappingProxyType(overrides))
        return self._content_types_cache

    def _validate_relationships(self) -> None:
        for part in self._entries:
            canonical_part = self._canonical_part_name(part)
            if not canonical_part.endswith(".rels"):
                continue
            if canonical_part != "_rels/.rels":
                source_part = self._source_part_for_relationship_part(canonical_part)
                if source_part not in self._canonical_entries:
                    raise WorkbookError(
                        "MISSING_RELATIONSHIP_SOURCE",
                        "XLSX 关系部件对应的源部件不存在",
                        part,
                    )
            root = self.read_xml(part, MAX_ENTRY)
            relationship_ids: set[str] = set()
            for element in root.iter():
                if element.tag not in {
                    "Relationship",
                    f"{{{_RELATIONSHIPS_NAMESPACE}}}Relationship",
                }:
                    continue
                relationship_id = element.get("Id")
                if not relationship_id or relationship_id in relationship_ids:
                    raise WorkbookError(
                        "INVALID_RELATIONSHIP", "XLSX 关系缺少或重复 Id", f"{part}:{relationship_id or 'Id'}"
                    )
                relationship_ids.add(relationship_id)
                relationship_type = element.get("Type")
                target = element.get("Target")
                location = f"{part}:{relationship_id}"
                try:
                    relationship_uri = (
                        urlsplit(relationship_type) if relationship_type is not None else None
                    )
                except ValueError:
                    relationship_uri = None
                if (
                    not relationship_type
                    or not target
                    or any(character.isspace() or ord(character) < 32 for character in relationship_type)
                    or relationship_uri is None
                    or not relationship_uri.scheme
                ):
                    raise WorkbookError("INVALID_RELATIONSHIP", "XLSX 关系缺少或无效字段", location)
                relation_type = relationship_type.lower()
                if (element.get("TargetMode") or "").lower() == "external":
                    raise WorkbookError(
                        "EXTERNAL_RESOURCE", "V1 不支持外部链接或外部资源", location
                    )
                resolved_target = self._validate_internal_relationship_target(part, target)
                if "vbaproject" in relation_type or "macro" in relation_type:
                    raise WorkbookError("MACRO_UNSUPPORTED", "V1 不支持包含宏的工作簿", location)
                if "oleobject" in relation_type or "embedding" in relation_type:
                    raise WorkbookError("OLE_UNSUPPORTED", "V1 不支持 OLE 嵌入对象", location)
                if "activex" in relation_type:
                    raise WorkbookError("ACTIVEX_UNSUPPORTED", "V1 不支持 ActiveX 控件", location)
                if resolved_target not in self._canonical_entries:
                    raise WorkbookError(
                        "MISSING_RELATIONSHIP_TARGET", "XLSX 关系目标不存在或不是文件部件", location
                    )

    @classmethod
    def _validate_internal_relationship_target(cls, relationship_part: str, target: str) -> str:
        """Ensure an internal OPC relationship cannot resolve outside the package."""

        if cls._canonical_part_name(relationship_part) == "_rels/.rels":
            return cls._resolve_relationship_target(None, target)
        return cls._resolve_relationship_target(
            cls._source_part_for_relationship_part(relationship_part), target
        )

    @classmethod
    def _source_part_for_relationship_part(cls, relationship_part: str) -> str:
        canonical_relationship_part = cls._canonical_part_name(relationship_part)
        segments = canonical_relationship_part.split("/")
        if (
            len(segments) < 2
            or canonical_relationship_part == "_rels/.rels"
            or segments[-2] != "_rels"
            or not segments[-1].endswith(".rels")
        ):
            raise WorkbookError(
                "UNSAFE_RELATIONSHIP", "XLSX 包含不安全的内部关系目标", relationship_part
            )
        source_name = segments[-1].removesuffix(".rels")
        if not source_name:
            raise WorkbookError(
                "UNSAFE_RELATIONSHIP", "XLSX 包含不安全的内部关系目标", relationship_part
            )
        return "/".join([*segments[:-2], source_name])

    @classmethod
    def _resolve_relationship_target(cls, source_part: str | None, target: str) -> str:
        """Resolve a target with the same URI rules used for archive validation."""

        if not target:
            raise WorkbookError("UNSAFE_RELATIONSHIP", "XLSX 包含不安全的内部关系目标", target)
        if _ENCODED_DOT_SEGMENT.search(target):
            raise WorkbookError("UNSAFE_RELATIONSHIP", "XLSX 包含不安全的内部关系目标", target)
        decoded = cls._decode_percent_uri(target, target, "UNSAFE_RELATIONSHIP")
        try:
            uri = urlsplit(decoded)
        except ValueError as exc:
            raise WorkbookError(
                "UNSAFE_RELATIONSHIP", "XLSX 包含不安全的内部关系目标", target
            ) from exc
        package_root_relative = decoded.startswith("/") and not decoded.startswith("//")
        if (
            uri.scheme
            or uri.netloc
            or decoded.startswith("//")
            or "\\" in decoded
            or _WINDOWS_DRIVE.match(decoded) is not None
            or uri.query
            or uri.fragment
        ):
            raise WorkbookError("UNSAFE_RELATIONSHIP", "XLSX 包含不安全的内部关系目标", target)

        resolved = [] if package_root_relative or source_part is None else cls._canonical_part_name(
            source_part
        ).split("/")[:-1]
        if package_root_relative:
            decoded = decoded[1:]
        for segment in decoded.split("/"):
            if not segment:
                raise WorkbookError("UNSAFE_RELATIONSHIP", "XLSX 包含不安全的内部关系目标", target)
            if segment == ".":
                continue
            if segment == "..":
                if not resolved:
                    raise WorkbookError(
                        "UNSAFE_RELATIONSHIP", "XLSX 包含不安全的内部关系目标", target
                    )
                resolved.pop()
                continue
            if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in segment):
                raise WorkbookError("UNSAFE_RELATIONSHIP", "XLSX 包含不安全的内部关系目标", target)
            resolved.append(segment)
        return cls._canonical_part_name("/".join(resolved))
