"""Process-local orchestration for the single-page invoice workflow.

The service layer keeps the completed XLSX engine and storage primitives
independent. Workbook bytes never enter SQLite; only the active source path
and the immutable parsed model live in process memory while a page is open.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fis.storage.batch_files import BatchFiles
from fis.xlsx.parser import ParsedBundle, parse_invoice_bundle

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_SOURCE_FILENAME = "source.xlsx"


class UploadRejected(Exception):
    """An upload was refused before or during safe ingestion.

    ``status_code`` is the HTTP status the web layer returns and ``message``
    is a user-facing Chinese explanation that never leaks internal detail.
    """

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


class WorkspaceStore:
    """Thread-safe, process-local registry of active workspace batches."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._batches: dict[str, WorkspaceBatch] = {}

    def put(self, batch: WorkspaceBatch) -> None:
        with self._lock:
            self._batches[batch.id] = batch

    def get(self, batch_id: str) -> WorkspaceBatch | None:
        with self._lock:
            return self._batches.get(batch_id)

    def remove(self, batch_id: str) -> None:
        with self._lock:
            self._batches.pop(batch_id, None)

    def active_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._batches)


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


class BatchService:
    """Coordinate safe upload, parse, and process-local workspace state."""

    def __init__(self, files: BatchFiles, store: WorkspaceStore) -> None:
        self._files = files
        self._store = store

    @property
    def store(self) -> WorkspaceStore:
        return self._store

    def get(self, batch_id: str) -> WorkspaceBatch | None:
        return self._store.get(batch_id)

    def create_from_upload(self, filename: str, chunks: Iterable[bytes]) -> WorkspaceBatch:
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
            self._store.put(batch)
            return batch
        except Exception:
            self._store.remove(batch_id)
            self._discard(batch_id)
            raise

    def _discard(self, batch_id: str) -> None:
        try:
            self._files.delete_batch(batch_id)
        except OSError:
            pass


__all__ = [
    "BatchService",
    "MAX_UPLOAD_BYTES",
    "UploadRejected",
    "WorkspaceBatch",
    "WorkspaceStore",
    "require_xlsx_name",
    "safe_display_name",
]
