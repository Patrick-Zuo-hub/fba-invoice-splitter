"""Bounded, read-only access to a safe XLSX OOXML package."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self
from zipfile import BadZipFile, ZipFile, ZipInfo

from lxml import etree  # type: ignore[import-untyped]

from fis.xlsx.errors import WorkbookError

MAX_ENTRIES = 5000
MAX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_ENTRY = 50 * 1024 * 1024

_CORE_PARTS = frozenset({"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml"})
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_XML_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    dtd_validation=False,
    huge_tree=False,
)


@dataclass(slots=True)
class SafePackage:
    """A validated XLSX archive that exposes bounded, read-only part access."""

    path: Path
    archive: ZipFile
    _entries: dict[str, ZipInfo] = field(default_factory=dict, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @classmethod
    def open(cls, path: Path | str) -> SafePackage:
        package_path = Path(path)
        if package_path.suffix.lower() != ".xlsx" or not package_path.is_file():
            raise WorkbookError("INVALID_PATH", "请选择存在的 .xlsx 工作簿文件")

        archive: ZipFile | None = None
        try:
            archive = ZipFile(package_path, "r")
            package = cls(path=package_path, archive=archive)
            package._validate()
            return package
        except WorkbookError:
            if archive is not None:
                archive.close()
            raise
        except (BadZipFile, EOFError, OSError, RuntimeError) as exc:
            if archive is not None:
                archive.close()
            raise WorkbookError("INVALID_XLSX", "文件不是有效的 XLSX 工作簿") from exc

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

        info = self._entries.get(part)
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
        except (BadZipFile, EOFError, OSError, RuntimeError) as exc:
            raise WorkbookError("INVALID_XLSX", "工作簿部件无法安全读取", part) from exc
        return b"".join(chunks)

    def read_xml(self, part: str, max_bytes: int) -> etree._Element:
        """Read and safely parse one XML part, rejecting DTDs and entities."""

        payload = self.read(part, max_bytes)
        if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
            raise WorkbookError("UNSAFE_XML", "工作簿 XML 不允许 DTD 或实体定义", part)
        try:
            return etree.fromstring(payload, parser=_XML_PARSER)
        except (etree.XMLSyntaxError, ValueError, TypeError) as exc:
            raise WorkbookError("INVALID_XML", "工作簿 XML 格式无效", part) from exc

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
        normalized_names: set[str] = set()
        for info in infos:
            normalized = self._validate_entry_name(info.filename)
            if normalized in normalized_names:
                raise WorkbookError("DUPLICATE_ENTRY", "XLSX 包含重复的文件条目", info.filename)
            normalized_names.add(normalized)
            if info.flag_bits & 0x1:
                raise WorkbookError("ENCRYPTED_ENTRY", "XLSX 不支持加密文件条目", info.filename)
            if info.file_size > MAX_ENTRY:
                raise WorkbookError("ENTRY_TOO_LARGE", "XLSX 包含超过大小限制的文件", info.filename)
            total_size += info.file_size
            if total_size > MAX_UNCOMPRESSED:
                raise WorkbookError("PACKAGE_TOO_LARGE", "XLSX 解压后的总大小超过限制")
            self._entries[info.filename] = info

        missing = _CORE_PARTS.difference(self._entries)
        if missing:
            raise WorkbookError("MISSING_CORE_PART", "XLSX 缺少必要的 OOXML 核心部件", min(missing))

        for part in self._entries:
            self._validate_unsupported_part(part)
        self._validate_content_types()
        self._validate_relationships()

    @staticmethod
    def _validate_entry_name(name: str) -> str:
        raw_parts = name.split("/")
        if (
            not name
            or "\\" in name
            or name.startswith(("/", "//"))
            or _WINDOWS_DRIVE.match(name) is not None
            or any(segment in {"", ".", ".."} for segment in raw_parts)
        ):
            raise WorkbookError("UNSAFE_PATH", "XLSX 包含不安全的文件路径", name)
        normalized = str(PurePosixPath(name))
        if normalized in {"", "."} or normalized.startswith("../"):
            raise WorkbookError("UNSAFE_PATH", "XLSX 包含不安全的文件路径", name)
        return normalized

    @staticmethod
    def _validate_unsupported_part(part: str) -> None:
        lowered = part.lower()
        if "vbaproject" in lowered or lowered.endswith(".vba"):
            raise WorkbookError("MACRO_UNSUPPORTED", "V1 不支持包含宏的工作簿", part)
        if "/embeddings/" in lowered or "oleobject" in lowered:
            raise WorkbookError("OLE_UNSUPPORTED", "V1 不支持 OLE 嵌入对象", part)
        if "/activex/" in lowered or "activex" in lowered:
            raise WorkbookError("ACTIVEX_UNSUPPORTED", "V1 不支持 ActiveX 控件", part)

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
            if not part.lower().endswith(".rels"):
                continue
            root = self.read_xml(part, MAX_ENTRY)
            for element in root.iter():
                relation_type = (element.get("Type") or "").lower()
                location = element.get("Target") or part
                if (element.get("TargetMode") or "").lower() == "external":
                    raise WorkbookError(
                        "EXTERNAL_RESOURCE", "V1 不支持外部链接或外部资源", location
                    )
                if "vbaproject" in relation_type or "macro" in relation_type:
                    raise WorkbookError("MACRO_UNSUPPORTED", "V1 不支持包含宏的工作簿", location)
                if "oleobject" in relation_type or "embedding" in relation_type:
                    raise WorkbookError("OLE_UNSUPPORTED", "V1 不支持 OLE 嵌入对象", location)
                if "activex" in relation_type:
                    raise WorkbookError("ACTIVEX_UNSUPPORTED", "V1 不支持 ActiveX 控件", location)
