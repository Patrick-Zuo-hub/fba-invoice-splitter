from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Self, cast
from urllib.request import ProxyHandler, build_opener

from filelock import FileLock, Timeout

MAX_RECORD_BYTES: Final = 4096
PRODUCT_MARKER: Final = "fba-invoice-splitter"
type Probe = Callable[[int], bool]


class InvalidInstanceRecord(ValueError):
    """The instance record cannot be trusted or parsed."""


class StartupError(RuntimeError):
    """The local FIS process could not start safely."""


@dataclass(frozen=True)
class InstanceRecord:
    pid: int
    port: int
    started_at: str

    def __post_init__(self) -> None:
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise InvalidInstanceRecord("pid must be a positive integer")
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise InvalidInstanceRecord("port must be an integer")
        if not 8765 <= self.port <= 8775:
            raise InvalidInstanceRecord("port is outside the FIS range")
        if not isinstance(self.started_at, str):
            raise InvalidInstanceRecord("started_at must be a string")
        try:
            parsed = datetime.fromisoformat(self.started_at)
        except ValueError as exc:
            raise InvalidInstanceRecord("started_at must be ISO 8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise InvalidInstanceRecord("started_at must include a timezone")


@dataclass(frozen=True)
class ExistingInstance:
    port: int


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInstanceRecord(f"duplicate key: {key}")
        result[key] = value
    return result


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def _ensure_runtime_directory(runtime_dir: Path) -> None:
    if runtime_dir.exists():
        if _is_link_or_junction(runtime_dir) or not runtime_dir.is_dir():
            raise StartupError("运行目录不是可信的普通目录")
    else:
        runtime_dir.mkdir(parents=True, mode=0o700)
    try:
        runtime_dir.chmod(0o700)
    except OSError:
        pass


def _validate_regular_file(path: Path) -> os.stat_result:
    try:
        details = path.lstat()
    except OSError as exc:
        raise InvalidInstanceRecord("instance record is unavailable") from exc
    if _is_link_or_junction(path) or not stat.S_ISREG(details.st_mode):
        raise InvalidInstanceRecord("instance record must be a regular file")
    return details


def read_instance_record(runtime_dir: Path) -> InstanceRecord:
    path = runtime_dir / "instance.json"
    details = _validate_regular_file(path)
    if details.st_size > MAX_RECORD_BYTES:
        raise InvalidInstanceRecord("instance record is too large")
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_RECORD_BYTES:
            raise InvalidInstanceRecord("instance record is too large")
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidInstanceRecord("instance record is malformed") from exc
    if not isinstance(value, dict) or set(value) != {"pid", "port", "started_at"}:
        raise InvalidInstanceRecord("instance record has unexpected fields")
    try:
        return InstanceRecord(
            pid=cast(int, value["pid"]),
            port=cast(int, value["port"]),
            started_at=cast(str, value["started_at"]),
        )
    except (TypeError, InvalidInstanceRecord) as exc:
        if isinstance(exc, InvalidInstanceRecord):
            raise
        raise InvalidInstanceRecord("instance record has invalid field types") from exc


def write_instance_record(runtime_dir: Path, record: InstanceRecord) -> None:
    _ensure_runtime_directory(runtime_dir)
    target = runtime_dir / "instance.json"
    payload = json.dumps(asdict(record), ensure_ascii=True, separators=(",", ":")).encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".instance-", dir=runtime_dir)
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def probe_fis(port: int, *, timeout: float = 0.35) -> bool:
    if not 8765 <= port <= 8775:
        return False
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            body = response.read(MAX_RECORD_BYTES + 1)
            return (
                cast(int, response.status) == 200
                and response.headers.get("X-FIS-Product") == PRODUCT_MARKER
                and len(body) <= MAX_RECORD_BYTES
                and json.loads(body) == {"status": "ok"}
            )
    except (OSError, ValueError, json.JSONDecodeError):
        return False


class InstanceLease:
    def __init__(self, runtime_dir: Path, lock: FileLock) -> None:
        self.runtime_dir = runtime_dir
        self._lock = lock
        self._owned_record: InstanceRecord | None = None
        self._closed = False

    def write_record(self, record: InstanceRecord) -> None:
        if self._closed:
            raise RuntimeError("instance lease is closed")
        write_instance_record(self.runtime_dir, record)
        self._owned_record = record

    def close(self) -> None:
        if self._closed:
            return
        path = self.runtime_dir / "instance.json"
        if self._owned_record is not None:
            try:
                if read_instance_record(self.runtime_dir) == self._owned_record:
                    path.unlink()
            except (FileNotFoundError, InvalidInstanceRecord, OSError):
                pass
        self._lock.release()
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def acquire_instance(
    runtime_dir: Path, *, probe: Probe = probe_fis
) -> InstanceLease | ExistingInstance:
    _ensure_runtime_directory(runtime_dir)
    lock_path = runtime_dir / "instance.lock"
    if (lock_path.exists() or lock_path.is_symlink()) and (
        _is_link_or_junction(lock_path) or not lock_path.is_file()
    ):
        raise StartupError("实例锁文件不可信")
    lock = FileLock(lock_path)
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        try:
            record = read_instance_record(runtime_dir)
        except (FileNotFoundError, InvalidInstanceRecord) as record_exc:
            raise StartupError("另一个实例持有锁，但无法确认其健康状态") from record_exc
        if probe(record.port):
            return ExistingInstance(record.port)
        raise StartupError("另一个实例持有锁，但无法确认其为健康的 FIS 实例") from exc

    lease = InstanceLease(runtime_dir, lock)
    record_path = runtime_dir / "instance.json"
    if record_path.exists() or record_path.is_symlink():
        try:
            record = read_instance_record(runtime_dir)
        except InvalidInstanceRecord:
            try:
                details = record_path.lstat()
            except OSError:
                details = None
            if details is None or not stat.S_ISREG(details.st_mode):
                lease.close()
                raise StartupError("旧实例记录不可信，无法安全清理")
            record_path.unlink()
        else:
            if probe(record.port):
                lease.close()
                return ExistingInstance(record.port)
            record_path.unlink()
    return lease
