from __future__ import annotations

import errno
import json
import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Self, cast
from urllib.request import ProxyHandler, build_opener

if os.name != "nt":
    import fcntl

MAX_RECORD_BYTES: Final = 4096
PRODUCT_MARKER: Final = "fba-invoice-splitter"
RECORD_NAME: Final = "instance.json"
LOCK_NAME: Final = "instance.lock"
type Probe = Callable[[int], bool]
type OperationHook = Callable[[str], None]
type FileIdentity = tuple[int, int]


class InvalidInstanceRecord(ValueError):
    """The instance record cannot be trusted or parsed."""


class _UnsafeInstanceFile(InvalidInstanceRecord):
    """The record's filesystem object is unsafe to access or remove."""


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


def _parse_record(raw: bytes) -> InstanceRecord:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidInstanceRecord("instance record is malformed") from exc
    if not isinstance(value, dict) or set(value) != {"pid", "port", "started_at"}:
        raise InvalidInstanceRecord("instance record has unexpected fields")
    return InstanceRecord(
        pid=cast(int, value["pid"]),
        port=cast(int, value["port"]),
        started_at=cast(str, value["started_at"]),
    )


def _identity(details: os.stat_result) -> FileIdentity:
    return details.st_dev, details.st_ino


class _PosixRuntimeDirectory:
    def __init__(
        self,
        runtime_dir: Path,
        parent_fd: int,
        directory_fd: int,
        operation_hook: OperationHook | None,
    ) -> None:
        self.path = runtime_dir
        self._parent_fd = parent_fd
        self._directory_fd = directory_fd
        self._directory_name = runtime_dir.name
        self._directory_identity = _identity(os.fstat(directory_fd))
        self._operation_hook = operation_hook
        self._closed = False

    @classmethod
    def open(
        cls,
        runtime_dir: Path,
        operation_hook: OperationHook | None,
        *,
        create: bool,
    ) -> _PosixRuntimeDirectory:
        required = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
        if os.name == "nt" or any(not hasattr(os, name) for name in required):
            raise StartupError("当前平台无法证明运行目录句柄安全，已拒绝启动")
        absolute = Path(os.path.abspath(runtime_dir))
        if absolute.parent == absolute:
            raise StartupError("运行目录路径无效")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        current_fd = os.open(os.sep, flags)
        try:
            parts = absolute.parts[1:]
            for index, component in enumerate(parts):
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, 0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise StartupError("运行目录包含符号链接或非目录对象") from exc
                    raise
                if index == len(parts) - 1:
                    directory = cls(absolute, current_fd, next_fd, operation_hook)
                    current_fd = -1
                    try:
                        directory._secure_directory_permissions()
                        directory.verify_identity()
                    except Exception:
                        directory.close()
                        raise
                    return directory
                os.close(current_fd)
                current_fd = next_fd
        finally:
            if current_fd >= 0:
                os.close(current_fd)
        raise StartupError("运行目录路径无效")

    def _hook(self, stage: str) -> None:
        if self._operation_hook is not None:
            self._operation_hook(stage)

    def _secure_directory_permissions(self) -> None:
        details = os.fstat(self._directory_fd)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_nlink < 1
        ):
            raise StartupError("运行目录所有者不可信")
        try:
            os.fchmod(self._directory_fd, 0o700)
        except OSError as exc:
            raise StartupError("无法收紧运行目录权限") from exc
        secured = os.fstat(self._directory_fd)
        if (
            not stat.S_ISDIR(secured.st_mode)
            or stat.S_IMODE(secured.st_mode) != 0o700
            or secured.st_uid != os.getuid()
            or secured.st_nlink < 1
        ):
            raise StartupError("运行目录权限不是当前用户专用")

    def verify_identity(self) -> None:
        opened = os.fstat(self._directory_fd)
        try:
            named = os.stat(
                self._directory_name,
                dir_fd=self._parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise StartupError("运行目录已被替换") from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or _identity(opened) != self._directory_identity
            or _identity(named) != self._directory_identity
        ):
            raise StartupError("运行目录已被替换")
        if (
            opened.st_uid != os.getuid()
            or named.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
            or stat.S_IMODE(named.st_mode) != 0o700
            or opened.st_nlink < 1
            or named.st_nlink < 1
        ):
            raise StartupError("运行目录权限不是当前用户专用")

    def _open_child(
        self,
        name: str,
        flags: int,
        *,
        label: str,
        hook_stage: str | None = None,
    ) -> tuple[int, os.stat_result]:
        if hook_stage is not None:
            self._hook(hook_stage)
        self.verify_identity()
        secure_flags = flags | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(name, secure_flags, 0o600, dir_fd=self._directory_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _UnsafeInstanceFile(f"{label} must not be a symlink") from exc
            raise
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise _UnsafeInstanceFile(f"{label} must be a regular file")
            if details.st_uid != os.getuid():
                raise _UnsafeInstanceFile(f"{label} has an unexpected owner")
            if details.st_nlink != 1:
                raise _UnsafeInstanceFile(f"{label} must not be a hard link")
            try:
                os.fchmod(descriptor, 0o600)
            except OSError as exc:
                raise StartupError(f"无法收紧{label}权限") from exc
            secured = os.fstat(descriptor)
            if stat.S_IMODE(secured.st_mode) != 0o600:
                raise StartupError(f"{label}权限不是当前用户专用")
            return descriptor, secured
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _read_bytes(descriptor: int, details: os.stat_result) -> bytes:
        if details.st_size > MAX_RECORD_BYTES:
            raise InvalidInstanceRecord("instance record is too large")
        chunks: list[bytes] = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_RECORD_BYTES:
            raise InvalidInstanceRecord("instance record is too large")
        return raw

    def read_record(self) -> tuple[InstanceRecord, FileIdentity]:
        descriptor, details = self._open_child(
            RECORD_NAME,
            os.O_RDONLY,
            label="instance record",
            hook_stage="before_record_open",
        )
        try:
            return _parse_record(self._read_bytes(descriptor, details)), _identity(details)
        finally:
            os.close(descriptor)

    def _existing_record_is_safe(self) -> None:
        try:
            descriptor, _ = self._open_child(
                RECORD_NAME, os.O_RDONLY, label="instance record"
            )
        except FileNotFoundError:
            return
        os.close(descriptor)

    def write_record(self, record: InstanceRecord) -> FileIdentity:
        self._existing_record_is_safe()
        payload = json.dumps(asdict(record), ensure_ascii=True, separators=(",", ":")).encode()
        temporary_name = f".instance-{secrets.token_hex(16)}"
        descriptor = -1
        published = False
        try:
            descriptor, temporary_details = self._open_child(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                label="temporary instance record",
            )
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written == 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(descriptor)
            self._hook("before_record_publish")
            self.verify_identity()
            try:
                published_source_fd, published_source_details = self._open_child(
                    temporary_name,
                    os.O_RDONLY,
                    label="temporary instance record",
                )
            except InvalidInstanceRecord as exc:
                raise StartupError("临时实例记录在发布时被替换") from exc
            os.close(published_source_fd)
            if _identity(published_source_details) != _identity(temporary_details):
                raise StartupError("临时实例记录在发布时被替换")
            os.replace(
                temporary_name,
                RECORD_NAME,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )
            published = True
            os.fsync(self._directory_fd)
            final_fd, final_details = self._open_child(
                RECORD_NAME, os.O_RDONLY, label="instance record"
            )
            os.close(final_fd)
            if _identity(final_details) != _identity(temporary_details):
                raise StartupError("实例记录在发布时被替换")
            self.verify_identity()
            return _identity(final_details)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not published:
                try:
                    os.unlink(temporary_name, dir_fd=self._directory_fd)
                except FileNotFoundError:
                    pass

    def record_exists(self) -> bool:
        self.verify_identity()
        try:
            os.stat(RECORD_NAME, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def remove_record(self, expected: FileIdentity | None = None) -> None:
        descriptor, details = self._open_child(
            RECORD_NAME, os.O_RDONLY, label="instance record"
        )
        os.close(descriptor)
        if expected is not None and _identity(details) != expected:
            return
        quarantine = f".stale-{secrets.token_hex(16)}"
        self._hook("before_record_cleanup")
        self.verify_identity()
        try:
            cleanup_fd, cleanup_details = self._open_child(
                RECORD_NAME, os.O_RDONLY, label="instance record"
            )
        except (FileNotFoundError, InvalidInstanceRecord) as exc:
            raise StartupError("实例记录在清理时被替换") from exc
        os.close(cleanup_fd)
        if _identity(cleanup_details) != _identity(details):
            raise StartupError("实例记录在清理时被替换")
        os.replace(
            RECORD_NAME,
            quarantine,
            src_dir_fd=self._directory_fd,
            dst_dir_fd=self._directory_fd,
        )
        quarantine_fd, quarantine_details = self._open_child(
            quarantine, os.O_RDONLY, label="quarantined instance record"
        )
        os.close(quarantine_fd)
        if _identity(quarantine_details) != _identity(details):
            raise StartupError("实例记录在清理时被替换")
        os.unlink(quarantine, dir_fd=self._directory_fd)
        os.fsync(self._directory_fd)

    def open_lock(self) -> int:
        try:
            descriptor, _ = self._open_child(
                LOCK_NAME,
                os.O_RDWR | os.O_CREAT,
                label="实例锁文件",
                hook_stage="before_lock_open",
            )
        except InvalidInstanceRecord as exc:
            raise StartupError("实例锁文件不可信") from exc
        return descriptor

    def close(self) -> None:
        if self._closed:
            return
        os.close(self._directory_fd)
        os.close(self._parent_fd)
        self._closed = True


def _open_runtime_directory(
    runtime_dir: Path, operation_hook: OperationHook | None, *, create: bool
) -> _PosixRuntimeDirectory:
    if os.name == "nt":
        raise StartupError("Windows 安全运行目录句柄与 ACL 无法证明，已拒绝启动")
    return _PosixRuntimeDirectory.open(runtime_dir, operation_hook, create=create)


def read_instance_record(
    runtime_dir: Path, *, _operation_hook: OperationHook | None = None
) -> InstanceRecord:
    directory = _open_runtime_directory(runtime_dir, _operation_hook, create=False)
    try:
        record, _ = directory.read_record()
        return record
    finally:
        directory.close()


def write_instance_record(
    runtime_dir: Path,
    record: InstanceRecord,
    *,
    _operation_hook: OperationHook | None = None,
) -> None:
    directory = _open_runtime_directory(runtime_dir, _operation_hook, create=True)
    try:
        directory.write_record(record)
    finally:
        directory.close()


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
    def __init__(self, directory: _PosixRuntimeDirectory, lock_descriptor: int) -> None:
        self.runtime_dir = directory.path
        self._directory = directory
        self._lock_descriptor = lock_descriptor
        self._owned_record: InstanceRecord | None = None
        self._owned_identity: FileIdentity | None = None
        self._closed = False

    def write_record(self, record: InstanceRecord) -> None:
        if self._closed:
            raise RuntimeError("instance lease is closed")
        self._owned_identity = self._directory.write_record(record)
        self._owned_record = record

    def close(self) -> None:
        if self._closed:
            return
        if self._owned_record is not None and self._owned_identity is not None:
            try:
                record, identity = self._directory.read_record()
                if record == self._owned_record and identity == self._owned_identity:
                    self._directory.remove_record(self._owned_identity)
            except (FileNotFoundError, InvalidInstanceRecord, OSError, StartupError):
                pass
        fcntl.flock(self._lock_descriptor, fcntl.LOCK_UN)
        os.close(self._lock_descriptor)
        self._directory.close()
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def acquire_instance(
    runtime_dir: Path,
    *,
    probe: Probe = probe_fis,
    _operation_hook: OperationHook | None = None,
) -> InstanceLease | ExistingInstance:
    directory = _open_runtime_directory(runtime_dir, _operation_hook, create=True)
    lock_descriptor = -1
    keep_resources = False
    try:
        lock_descriptor = directory.open_lock()
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            try:
                record, _ = directory.read_record()
            except (FileNotFoundError, InvalidInstanceRecord) as record_exc:
                raise StartupError("另一个实例持有锁，但无法确认其健康状态") from record_exc
            if probe(record.port):
                return ExistingInstance(record.port)
            raise StartupError("另一个实例持有锁，但无法确认其为健康的 FIS 实例") from exc

        lease = InstanceLease(directory, lock_descriptor)
        if directory.record_exists():
            try:
                record, identity = directory.read_record()
            except _UnsafeInstanceFile as exc:
                raise StartupError("旧实例记录不可信，无法安全清理") from exc
            except InvalidInstanceRecord:
                directory.remove_record()
            else:
                if probe(record.port):
                    lease.close()
                    lock_descriptor = -1
                    return ExistingInstance(record.port)
                directory.remove_record(identity)
        keep_resources = True
        return lease
    finally:
        if not keep_resources:
            if lock_descriptor >= 0:
                os.close(lock_descriptor)
            if not directory._closed:
                directory.close()
