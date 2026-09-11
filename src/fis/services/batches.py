"""Process-local orchestration for the single-page invoice workflow.

The service layer keeps the completed XLSX engine and storage primitives
independent. Workbook bytes never enter SQLite; only the active source path,
the immutable parsed model, and the in-progress edits live in process memory
while a page is open.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from fis.domain.rules import default_dates, parse_warehouse_mapping
from fis.platform.folders import (
    FolderOpener,
    FolderPicker,
    NativeFolderOpener,
    NativeFolderPicker,
    PlatformError,
)
from fis.storage.batch_files import BatchFiles
from fis.storage.database import Database
from fis.storage.repositories import (
    BatchRepository,
    DefaultsRepository,
    HistoryEntry,
    MasterDataRepository,
)
from fis.xlsx.errors import WorkbookError
from fis.xlsx.generator import (
    ShipmentOutput,
    generate_shipment_workbooks,
    safe_filename,
)
from fis.xlsx.parser import ParsedBundle, parse_invoice_bundle

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_SOURCE_FILENAME = "source.xlsx"

_TEXT_FIELDS = ("warehouse_code", "warehouse_address", "channel", "delivery_warehouse")
_DATE_FIELDS = ("delivery_date", "sailing_date")
_ALL_SUFFIX = "_all"
_DEFAULT_CHANNEL_KEY = "channel"


class UploadRejected(Exception):
    """An upload was refused before or during safe ingestion."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True, slots=True)
class WorkspaceBatch:
    """One active, immutable upload held for the open page."""

    id: str
    source_path: Path
    source_filename: str
    source_sha256: str
    parsed: ParsedBundle

    @property
    def shipment_count(self) -> int:
        return len(self.parsed.shipments)


@dataclass(frozen=True, slots=True)
class ShipmentEdit:
    """A sparse overlay of confirmed logistics values."""

    warehouse_code: str | None = None
    warehouse_address: str | None = None
    channel: str | None = None
    delivery_date: date | None = None
    sailing_date: date | None = None
    delivery_warehouse: str | None = None


@dataclass
class Workspace:
    """Mutable per-page session state wrapping one immutable upload."""

    batch: WorkspaceBatch
    common: ShipmentEdit = ShipmentEdit()
    overrides: dict[str, ShipmentEdit] = field(default_factory=dict)
    output_directory: Path | None = None
    # Scope key ``None`` is the common scope; a shipment id is an override scope.
    sailing_manual: dict[str | None, bool] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.batch.id

    @property
    def source_path(self) -> Path:
        return self.batch.source_path

    @property
    def source_filename(self) -> str:
        return self.batch.source_filename

    @property
    def parsed(self) -> ParsedBundle:
        return self.batch.parsed

    @property
    def shipment_count(self) -> int:
        return self.batch.shipment_count

    @property
    def shipment_ids(self) -> tuple[str, ...]:
        return tuple(shipment.shipment_id for shipment in self.batch.parsed.shipments)


class WorkspaceStore:
    """Thread-safe, process-local registry of active workspaces."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workspaces: dict[str, Workspace] = {}

    def put(self, workspace: Workspace) -> None:
        with self._lock:
            self._workspaces[workspace.id] = workspace

    def get(self, batch_id: str) -> Workspace | None:
        with self._lock:
            return self._workspaces.get(batch_id)

    def remove(self, batch_id: str) -> None:
        with self._lock:
            self._workspaces.pop(batch_id, None)

    def active_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._workspaces)


def require_xlsx_name(filename: str) -> None:
    if not filename.strip().lower().endswith(".xlsx"):
        raise UploadRejected(422, "请上传 .xlsx 格式的发票文件")


def safe_display_name(filename: str) -> str:
    base = filename.replace("\\", "/").split("/")[-1].strip()
    printable = "".join(character for character in base if character.isprintable())
    return printable or "invoice.xlsx"


def _stream_to_file(destination: Path, chunks: Iterable[bytes], limit: int) -> str:
    hasher = hashlib.sha256()
    written = 0
    with open(destination, "wb") as handle:
        for chunk in chunks:
            written += len(chunk)
            if written > limit:
                raise UploadRejected(413, "发票文件超过 50 MiB 上限")
            hasher.update(chunk)
            handle.write(chunk)
    if written == 0:
        raise UploadRejected(422, "发票文件为空")
    return hasher.hexdigest()


def _coerce_date(raw: object, errors: dict[str, str], field_name: str) -> tuple[str, date | None]:
    """Return ("set"|"clear"|"error", value); record an error for bad input."""

    text = "" if raw is None else str(raw).strip()
    if text == "":
        return "clear", None
    try:
        return "set", date.fromisoformat(text)
    except ValueError:
        errors[field_name] = "日期格式应为 YYYY-MM-DD"
        return "error", None


# Field labels used in the complete field-error mapping and the editor view.
FIELD_LABELS = {
    "warehouse_code": "FBA 仓库编码",
    "warehouse_address": "FBA 仓库地址",
    "channel": "运输渠道",
    "delivery_date": "送仓日期",
    "sailing_date": "开船日期",
    "delivery_warehouse": "送货仓库",
}


GenerateEngine = Callable[[str, tuple[ShipmentOutput, ...], Path], tuple[Path, ...]]


class GenerationError(Exception):
    """Generation was blocked or failed; carries a user-facing message."""

    status_code = 422

    def __init__(self, message: str, *, field_errors: Mapping[str, Mapping[str, str]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field_errors = field_errors or {}


@dataclass(frozen=True, slots=True)
class GenerationResult:
    batch_id: str
    output_directory: Path
    filenames: tuple[str, ...]


class BatchService:
    """Coordinate safe upload, parse, edits, defaults, and folder selection."""

    def __init__(
        self,
        files: BatchFiles,
        store: WorkspaceStore,
        *,
        database: Database | None = None,
        folder_picker: FolderPicker | None = None,
        folder_opener: FolderOpener | None = None,
        engine: GenerateEngine | None = None,
        today: Callable[[], date] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._files = files
        self._store = store
        self._defaults = DefaultsRepository(database) if database is not None else None
        self._masters = MasterDataRepository(database) if database is not None else None
        self._batches = BatchRepository(database) if database is not None else None
        self._picker: FolderPicker = folder_picker or NativeFolderPicker()
        self._opener: FolderOpener = folder_opener or NativeFolderOpener()
        self._engine: GenerateEngine = engine or generate_shipment_workbooks
        self._today = today or date.today
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def store(self) -> WorkspaceStore:
        return self._store

    def get(self, batch_id: str) -> Workspace | None:
        return self._store.get(batch_id)

    def require(self, batch_id: str) -> Workspace:
        workspace = self._store.get(batch_id)
        if workspace is None:
            raise KeyError(batch_id)
        return workspace

    # -- upload -----------------------------------------------------------

    def create_from_upload(self, filename: str, chunks: Iterable[bytes]) -> Workspace:
        require_xlsx_name(filename)
        batch_id = str(uuid4())
        work = self._files.create(batch_id)
        try:
            source = work / _SOURCE_FILENAME
            digest = _stream_to_file(source, chunks, MAX_UPLOAD_BYTES)
            parsed = parse_invoice_bundle(source)
            batch = WorkspaceBatch(
                id=batch_id,
                source_path=source,
                source_filename=safe_display_name(filename),
                source_sha256=digest,
                parsed=parsed,
            )
            workspace = Workspace(batch=batch)
            self._store.put(workspace)
            return workspace
        except Exception:
            self._store.remove(batch_id)
            self._discard(batch_id)
            raise

    def _discard(self, batch_id: str) -> None:
        try:
            self._files.delete_batch(batch_id)
        except OSError:
            pass

    # -- edits ------------------------------------------------------------

    def apply_fields(
        self,
        batch_id: str,
        common_raw: Mapping[str, object],
        override_raw: Mapping[str, Mapping[str, object]],
    ) -> tuple[Workspace, dict[str, dict[str, str]]]:
        workspace = self.require(batch_id)
        errors: dict[str, dict[str, str]] = {}
        known = set(workspace.shipment_ids)

        common_errors: dict[str, str] = {}
        workspace.common = self._merge_scope(
            workspace.common, common_raw, None, workspace.sailing_manual, common_errors
        )
        if common_errors:
            errors["common"] = common_errors

        for shipment_id, raw in override_raw.items():
            if shipment_id not in known:
                errors.setdefault(shipment_id, {})["shipment"] = "未知货件编号"
                continue
            scope_errors: dict[str, str] = {}
            current = workspace.overrides.get(shipment_id, ShipmentEdit())
            workspace.overrides[shipment_id] = self._merge_scope(
                current, raw, shipment_id, workspace.sailing_manual, scope_errors
            )
            if scope_errors:
                errors[shipment_id] = scope_errors
        return workspace, errors

    def _merge_scope(
        self,
        target: ShipmentEdit,
        raw: Mapping[str, object],
        scope: str | None,
        manual: dict[str | None, bool],
        errors: dict[str, str],
    ) -> ShipmentEdit:
        updates: dict[str, Any] = {}
        for field_name in _TEXT_FIELDS:
            if field_name in raw:
                text = "" if raw[field_name] is None else str(raw[field_name]).strip()
                updates[field_name] = text or None

        delivery_changed = False
        if "delivery_date" in raw:
            kind, value = _coerce_date(raw["delivery_date"], errors, "delivery_date")
            if kind == "set":
                updates["delivery_date"] = value
                delivery_changed = True
            elif kind == "clear":
                updates["delivery_date"] = None

        if "sailing_date" in raw:
            kind, value = _coerce_date(raw["sailing_date"], errors, "sailing_date")
            if kind == "set":
                updates["sailing_date"] = value
                manual[scope] = True
            elif kind == "clear":
                updates["sailing_date"] = None
                manual[scope] = False

        merged = replace(target, **updates)
        if delivery_changed and not manual.get(scope, False):
            delivery = merged.delivery_date
            if delivery is not None:
                merged = replace(merged, sailing_date=delivery + timedelta(days=7))
        return merged

    # -- resolution -------------------------------------------------------

    def _default_channel(self, warehouse_code: str | None) -> str | None:
        if self._defaults is not None and warehouse_code:
            remembered = self._defaults.get(f"{_DEFAULT_CHANNEL_KEY}:{warehouse_code}")
            if remembered:
                return remembered
        if self._defaults is not None:
            return self._defaults.get(_DEFAULT_CHANNEL_KEY)
        return None

    def _lookup_address(self, warehouse_code: str | None) -> str | None:
        if self._masters is None or not warehouse_code:
            return None
        warehouse = self._masters.get_fba_warehouse(warehouse_code)
        return warehouse.address if warehouse is not None else None

    def apply_mapping(
        self, batch_id: str, text: str
    ) -> tuple[Workspace, dict[str, object]]:
        workspace = self.require(batch_id)
        mapping = parse_warehouse_mapping(text)
        known = set(workspace.shipment_ids)
        applied: dict[str, str] = {}
        unknown: list[str] = []
        for shipment_id, code in mapping.items():
            if shipment_id in known:
                current = workspace.overrides.get(shipment_id, ShipmentEdit())
                workspace.overrides[shipment_id] = replace(current, warehouse_code=code)
                applied[shipment_id] = code
            else:
                unknown.append(shipment_id)
        unmapped = [
            shipment_id
            for shipment_id in workspace.shipment_ids
            if not (self.effective_fields(workspace, shipment_id)["warehouse_code"])
        ]
        return workspace, {"applied": applied, "unknown": unknown, "unmapped": unmapped}

    def effective_fields(self, workspace: Workspace, shipment_id: str) -> dict[str, object]:
        common = workspace.common
        override = workspace.overrides.get(shipment_id)

        def pick(name: str) -> Any:
            if override is not None:
                value = getattr(override, name)
                if value is not None:
                    return value
            return getattr(common, name)

        warehouse_code = pick("warehouse_code")
        code = warehouse_code if isinstance(warehouse_code, str) and warehouse_code else None
        delivery, sailing_default = default_dates(self._today())
        delivery_date = pick("delivery_date") or delivery
        sailing_date = pick("sailing_date")
        if sailing_date is None:
            sailing_date = delivery_date + timedelta(days=7) if delivery_date else sailing_default
        channel = pick("channel") or self._default_channel(code)
        return {
            "warehouse_code": warehouse_code,
            # Address is always derived from Master Data by code, never typed.
            "warehouse_address": self._lookup_address(code),
            "channel": channel,
            "delivery_date": delivery_date,
            "sailing_date": sailing_date,
            "delivery_warehouse": pick("delivery_warehouse"),
        }

    def resolve_output(
        self, workspace: Workspace, shipment_id: str
    ) -> tuple[ShipmentOutput | None, dict[str, str]]:
        fields = self.effective_fields(workspace, shipment_id)
        errors: dict[str, str] = {}
        code = fields.get("warehouse_code")
        if not code:
            errors["warehouse_code"] = "未指派 FBA 仓库代码"
        elif not fields.get("warehouse_address"):
            errors["warehouse_address"] = f"仓库代码 {code} 未在主数据维护地址"
        if not fields.get("channel"):
            errors["channel"] = "运输渠道必填"
        if not fields.get("delivery_date"):
            errors["delivery_date"] = "送仓日期必填"
        if not fields.get("sailing_date"):
            errors["sailing_date"] = "开船日期必填"
        if not fields.get("delivery_warehouse"):
            errors["delivery_warehouse"] = "送货仓库必填"
        if errors:
            return None, errors
        return (
            ShipmentOutput(
                shipment_id=shipment_id,
                warehouse_code=str(fields["warehouse_code"]),
                warehouse_address=str(fields["warehouse_address"]),
                channel=str(fields["channel"]),
                delivery_date=fields["delivery_date"],  # type: ignore[arg-type]
                sailing_date=fields["sailing_date"],  # type: ignore[arg-type]
                delivery_warehouse=str(fields["delivery_warehouse"]),
            ),
            {},
        )

    def resolve_all(
        self, workspace: Workspace
    ) -> tuple[tuple[ShipmentOutput, ...], dict[str, dict[str, str]]]:
        outputs: list[ShipmentOutput] = []
        errors: dict[str, dict[str, str]] = {}
        for shipment_id in workspace.shipment_ids:
            output, field_errors = self.resolve_output(workspace, shipment_id)
            if output is None:
                errors[shipment_id] = field_errors
            else:
                outputs.append(output)
        return tuple(outputs), errors

    def editor_view(
        self, workspace: Workspace, errors: Mapping[str, Mapping[str, str]] | None = None
    ) -> dict[str, object]:
        errors = errors or {}
        rows: list[dict[str, object]] = []
        for shipment in workspace.parsed.shipments:
            fields = self.effective_fields(workspace, shipment.shipment_id)
            product_names = tuple(
                dict.fromkeys(
                    name
                    for item in shipment.items
                    if (name := item.cells.get("E", "").strip())
                )
            )
            carton_total = Decimal(0)
            for item in shipment.items:
                try:
                    carton_total += Decimal(item.cells.get("K", "0").strip() or "0")
                except InvalidOperation:
                    continue
            rows.append(
                {
                    "shipment_id": shipment.shipment_id,
                    "item_count": len(shipment.items),
                    "carton_count": (
                        int(carton_total)
                        if carton_total == carton_total.to_integral_value()
                        else str(carton_total.normalize())
                    ),
                    "product_names": product_names,
                    "product_summary": _product_summary(product_names),
                    "fields": {name: _display(value) for name, value in fields.items()},
                    "errors": dict(errors.get(shipment.shipment_id, {})),
                }
            )
        return {
            "batch_id": workspace.id,
            "rows": rows,
            "common_errors": dict(errors.get("common", {})),
            "output_directory": str(workspace.output_directory)
            if workspace.output_directory is not None
            else None,
        }

    # -- output folder ----------------------------------------------------

    def choose_output_folder(self, batch_id: str) -> tuple[Workspace, bool]:
        workspace = self.require(batch_id)
        chosen = self._picker.choose(workspace.output_directory)
        if chosen is None:
            return workspace, False
        if not os.access(chosen, os.W_OK):
            raise PlatformError("所选文件夹不可写入")
        workspace.output_directory = chosen
        return workspace, True


    # -- generation -------------------------------------------------------

    def generate(self, batch_id: str) -> GenerationResult:
        workspace = self.require(batch_id)
        destination = workspace.output_directory
        if destination is None:
            raise GenerationError("请先选择输出文件夹")
        if not destination.is_dir() or not os.access(destination, os.W_OK):
            raise GenerationError("输出文件夹不存在或不可写入")

        outputs, field_errors = self.resolve_all(workspace)
        if field_errors:
            raise GenerationError(_summarize_field_errors(field_errors), field_errors=field_errors)
        if not outputs:
            raise GenerationError("没有可生成的货件")

        try:
            names = [safe_filename(output) for output in outputs]
        except WorkbookError as error:
            raise GenerationError(str(error) or "无法生成安全文件名") from error
        if len({name.casefold() for name in names}) != len(names):
            raise GenerationError("多个货件会生成相同的文件名")
        clashes = [name for name in names if (destination / name).exists()]
        if clashes:
            raise GenerationError("输出文件夹中已存在同名文件：" + "、".join(clashes))

        now = self._now()
        try:
            paths = self._engine(str(workspace.source_path), tuple(outputs), destination)
        except WorkbookError as error:
            self._record_failed(workspace, error.code, str(error), now)
            raise GenerationError(str(error) or "生成失败") from error
        except Exception as error:
            self._record_failed(workspace, "INTERNAL", None, now)
            raise GenerationError("生成过程中发生内部错误") from error

        self._record_completed(workspace, outputs, paths, now)
        self._store_defaults(workspace, outputs)
        return GenerationResult(batch_id, destination, tuple(path.name for path in paths))

    def _snapshot_json(self, output: ShipmentOutput) -> str:
        return json.dumps(
            {
                "shipment_id": output.shipment_id,
                "warehouse_code": output.warehouse_code,
                "warehouse_address": output.warehouse_address,
                "channel": output.channel,
                "delivery_date": output.delivery_date.isoformat(),
                "sailing_date": output.sailing_date.isoformat(),
                "delivery_warehouse": output.delivery_warehouse,
            },
            ensure_ascii=False,
        )

    def _record_completed(
        self,
        workspace: Workspace,
        outputs: tuple[ShipmentOutput, ...],
        paths: tuple[Path, ...],
        now: datetime,
    ) -> None:
        if self._batches is None:
            return
        shipments = [
            (output.shipment_id, self._snapshot_json(output), str(path))
            for output, path in zip(outputs, paths, strict=True)
        ]
        item_count = sum(len(shipment.items) for shipment in workspace.parsed.shipments)
        self._batches.record_completed(
            batch_id=workspace.id,
            filename=workspace.source_filename,
            sha256=workspace.batch.source_sha256,
            source_path=str(workspace.source_path),
            shipment_count=len(outputs),
            item_count=item_count,
            shipments=shipments,
            now=now,
        )

    def _record_failed(
        self, workspace: Workspace, error_code: str, error_detail: str | None, now: datetime
    ) -> None:
        if self._batches is None:
            return
        self._batches.record_failed(
            batch_id=workspace.id,
            filename=workspace.source_filename,
            sha256=workspace.batch.source_sha256,
            source_path=str(workspace.source_path),
            shipment_count=workspace.shipment_count,
            error_code=error_code,
            error_detail=error_detail,
            now=now,
        )

    def _store_defaults(
        self, workspace: Workspace, outputs: tuple[ShipmentOutput, ...]
    ) -> None:
        if self._defaults is None:
            return
        # Remember the channel chosen for each warehouse code, and the common
        # delivery warehouse, so the next batch pre-fills the same choices.
        for output in outputs:
            self._defaults.set(f"{_DEFAULT_CHANNEL_KEY}:{output.warehouse_code}", output.channel)
        if workspace.common.delivery_warehouse:
            self._defaults.set("delivery_warehouse", workspace.common.delivery_warehouse)

    # -- history ----------------------------------------------------------

    def history_entries(self) -> list[HistoryEntry]:
        if self._batches is None:
            return []
        return self._batches.list_history_entries()

    def open_history_folder(self, batch_id: str) -> None:
        if self._batches is None:
            raise KeyError(batch_id)
        directory = self._batches.output_directory(batch_id)
        if directory is None:
            raise KeyError(batch_id)
        self._opener.open(Path(directory))



def _product_summary(names: tuple[str, ...]) -> str:
    if len(names) <= 2:
        return "、".join(names)
    return f"{names[0]}、{names[1]}，等 {len(names)} 种产品"


def _display(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _summarize_field_errors(errors: Mapping[str, Mapping[str, str]]) -> str:
    parts: list[str] = []
    for shipment_id, fields in errors.items():
        labels = "、".join(fields.values())
        parts.append(f"{shipment_id}：{labels}")
    return "以下货件信息不完整 —— " + "；".join(parts)


__all__ = [
    "FIELD_LABELS",
    "MAX_UPLOAD_BYTES",
    "BatchService",
    "GenerationError",
    "GenerationResult",
    "ShipmentEdit",
    "UploadRejected",
    "Workspace",
    "WorkspaceBatch",
    "WorkspaceStore",
    "require_xlsx_name",
    "safe_display_name",
]
