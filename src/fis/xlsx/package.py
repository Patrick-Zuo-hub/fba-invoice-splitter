"""Bounded, read-only access to a safe XLSX OOXML package."""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self
from urllib.parse import unquote_to_bytes, urlsplit
from zipfile import BadZipFile, ZipFile, ZipInfo

from lxml import etree  # type: ignore[import-untyped]

from fis.xlsx.errors import WorkbookError

MAX_ENTRIES = 5000
MAX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_ENTRY = 50 * 1024 * 1024

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ENCODED_SEPARATOR = re.compile(r"%(?:2[fF]|5[cC])")
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
_ARCHIVE_ERRORS = (BadZipFile, EOFError, OSError, RuntimeError, zlib.error)
_RELATIONSHIPS_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass(slots=True)
class SafePackage:
    """A validated XLSX archive that exposes bounded, read-only part access."""

    path: Path
    archive: ZipFile
    _entries: dict[str, ZipInfo] = field(default_factory=dict, init=False, repr=False)
    _canonical_entries: dict[str, ZipInfo] = field(default_factory=dict, init=False, repr=False)
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
            canonical_name = self._canonical_part_name(info.filename)
            if canonical_name in canonical_names:
                raise WorkbookError("DUPLICATE_ENTRY", "XLSX 包含重复的文件条目", info.filename)
            canonical_names.add(canonical_name)
            if info.flag_bits & 0x1:
                raise WorkbookError("ENCRYPTED_ENTRY", "XLSX 不支持加密文件条目", info.filename)
            if info.file_size > MAX_ENTRY:
                raise WorkbookError("ENTRY_TOO_LARGE", "XLSX 包含超过大小限制的文件", info.filename)
            total_size += info.file_size
            if total_size > MAX_UNCOMPRESSED:
                raise WorkbookError("PACKAGE_TOO_LARGE", "XLSX 解压后的总大小超过限制")
            self._entries[info.filename] = info
            self._canonical_entries[canonical_name] = info

        missing = _CANONICAL_CORE_PARTS.difference(self._canonical_entries)
        if missing:
            raise WorkbookError("MISSING_CORE_PART", "XLSX 缺少必要的 OOXML 核心部件", min(missing))

        for part, info in self._entries.items():
            self._validate_unsupported_part(self._canonical_part_name(part), info.filename)
        self._validate_content_types()
        self._validate_relationships()

    @staticmethod
    def _decode_percent_uri(value: str, location: str, error_code: str) -> str:
        """Decode percent escapes repeatedly while rejecting dangerous URI syntax."""

        current = value
        first_pass = True
        while "%" in current:
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
        root = self.read_xml("[Content_Types].xml", MAX_ENTRY)
        for element in root.iter():
            content_type = (element.get("ContentType") or "").lower()
            location = element.get("PartName") or "[Content_Types].xml"
            if "macroenabled" in content_type or "vbaproject" in content_type:
                raise WorkbookError("MACRO_UNSUPPORTED", "V1 不支持包含宏的工作簿", location)
            if "oleobject" in content_type or "/embedding" in content_type:
                raise WorkbookError("OLE_UNSUPPORTED", "V1 不支持 OLE 嵌入对象", location)
            if "activex" in content_type:
                raise WorkbookError("ACTIVEX_UNSUPPORTED", "V1 不支持 ActiveX 控件", location)

    def _validate_relationships(self) -> None:
        for part in self._entries:
            if not self._canonical_part_name(part).endswith(".rels"):
                continue
            root = self.read_xml(part, MAX_ENTRY)
            for element in root.iter():
                if element.tag not in {
                    "Relationship",
                    f"{{{_RELATIONSHIPS_NAMESPACE}}}Relationship",
                }:
                    continue
                relation_type = (element.get("Type") or "").lower()
                target = element.get("Target") or ""
                location = target or part
                if (element.get("TargetMode") or "").lower() == "external":
                    raise WorkbookError(
                        "EXTERNAL_RESOURCE", "V1 不支持外部链接或外部资源", location
                    )
                self._validate_internal_relationship_target(part, target)
                if "vbaproject" in relation_type or "macro" in relation_type:
                    raise WorkbookError("MACRO_UNSUPPORTED", "V1 不支持包含宏的工作簿", location)
                if "oleobject" in relation_type or "embedding" in relation_type:
                    raise WorkbookError("OLE_UNSUPPORTED", "V1 不支持 OLE 嵌入对象", location)
                if "activex" in relation_type:
                    raise WorkbookError("ACTIVEX_UNSUPPORTED", "V1 不支持 ActiveX 控件", location)

    @classmethod
    def _validate_internal_relationship_target(cls, relationship_part: str, target: str) -> None:
        """Ensure an internal OPC relationship cannot resolve outside the package."""

        if not target:
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

        relationship_segments = cls._canonical_part_name(relationship_part).split("/")
        if len(relationship_segments) < 2 or relationship_segments[-2] != "_rels":
            raise WorkbookError("UNSAFE_RELATIONSHIP", "XLSX 包含不安全的内部关系目标", target)
        resolved = [] if package_root_relative else relationship_segments[:-2]
        if package_root_relative:
            decoded = decoded[1:]
        for segment in decoded.split("/"):
            if not segment or segment == ".":
                raise WorkbookError("UNSAFE_RELATIONSHIP", "XLSX 包含不安全的内部关系目标", target)
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
