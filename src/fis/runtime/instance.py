from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import ntpath
import os
import re
import secrets
import stat
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Final, Protocol, Self, cast
from urllib.request import ProxyHandler, build_opener

if sys.platform != "win32":
    import fcntl

MAX_RECORD_BYTES: Final = 4096
PRODUCT_MARKER: Final = "fba-invoice-splitter"
RECORD_NAME: Final = "instance.json"
LOCK_NAME: Final = "instance.lock"
type Probe = Callable[[int], bool]
"""What acquire_instance needs: is this port a live instance of ours?"""


class HealthProbe(Protocol):
    """The richer contract probe_fis itself offers, identity included."""

    def __call__(self, port: int, *, identity: str | None = None) -> bool: ...

type OperationHook = Callable[[str], None]
type FileIdentity = tuple[int, int]


@dataclass(frozen=True)
class _WindowsFileInfo:
    identity: FileIdentity
    final_path: str
    is_directory: bool
    is_reparse: bool
    link_count: int
    size: int
    acl_safe: bool
    acl_detail: str = ""
    """Rendered vs expected SDDL, populated only when ``acl_safe`` is False."""


class _WindowsApi(Protocol):
    def validate_local_path(self, path: Path) -> Path: ...
    def create_directory(self, path: Path) -> None: ...
    def open_directory(self, path: Path) -> tuple[int, _WindowsFileInfo]: ...
    def open_file(
        self, path: Path, *, access: str, creation: str
    ) -> tuple[int, _WindowsFileInfo]: ...
    def info(self, handle: int) -> _WindowsFileInfo: ...
    def read(self, handle: int, limit: int) -> bytes: ...
    def write(self, handle: int, payload: bytes) -> None: ...
    def flush(self, handle: int) -> None: ...
    def replace(self, source: Path, target: Path) -> None: ...
    def delete(self, path: Path, expected: FileIdentity, expected_parent: str) -> None: ...
    def lock(self, handle: int) -> bool: ...
    def unlock(self, handle: int) -> None: ...
    def close(self, handle: int) -> None: ...


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
        if sys.platform == "win32" or any(not hasattr(os, name) for name in required):
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
            or details.st_uid != os.getuid()  # type: ignore[attr-defined, unused-ignore]
            or details.st_nlink < 1
        ):
            raise StartupError("运行目录所有者不可信")
        try:
            os.fchmod(self._directory_fd, 0o700)  # type: ignore[attr-defined, unused-ignore]
        except OSError as exc:
            raise StartupError("无法收紧运行目录权限") from exc
        secured = os.fstat(self._directory_fd)
        if (
            not stat.S_ISDIR(secured.st_mode)
            or stat.S_IMODE(secured.st_mode) != 0o700
            or secured.st_uid != os.getuid()  # type: ignore[attr-defined, unused-ignore]
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
            opened.st_uid != os.getuid()  # type: ignore[attr-defined, unused-ignore]
            or named.st_uid != os.getuid()  # type: ignore[attr-defined, unused-ignore]
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
        secure_flags = flags | os.O_NOFOLLOW | os.O_CLOEXEC  # type: ignore[attr-defined, unused-ignore]
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
            if details.st_uid != os.getuid():  # type: ignore[attr-defined, unused-ignore]
                raise _UnsafeInstanceFile(f"{label} has an unexpected owner")
            if details.st_nlink != 1:
                raise _UnsafeInstanceFile(f"{label} must not be a hard link")
            try:
                os.fchmod(descriptor, 0o600)  # type: ignore[attr-defined, unused-ignore]
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
            descriptor, _ = self._open_child(RECORD_NAME, os.O_RDONLY, label="instance record")
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
        descriptor, details = self._open_child(RECORD_NAME, os.O_RDONLY, label="instance record")
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

    @staticmethod
    def lock(handle: int) -> bool:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[name-defined, unused-ignore]
        except BlockingIOError:
            return False
        return True

    @staticmethod
    def unlock(handle: int) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)  # type: ignore[name-defined, unused-ignore]

    @staticmethod
    def close_lock(handle: int) -> None:
        os.close(handle)

    def close(self) -> None:
        if self._closed:
            return
        os.close(self._directory_fd)
        os.close(self._parent_fd)
        self._closed = True


def _windows_normalize_final_path(value: str) -> str:
    normalized = value
    if normalized.startswith("\\\\?\\UNC\\"):
        normalized = "\\\\" + normalized[8:]
    elif normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    return ntpath.normcase(ntpath.normpath(normalized))


class _WindowsRuntimeDirectory:
    def __init__(
        self,
        runtime_dir: Path,
        directory_handle: int,
        directory_info: _WindowsFileInfo,
        operation_hook: OperationHook | None,
        api: _WindowsApi,
    ) -> None:
        self.path = runtime_dir
        self._directory_handle = directory_handle
        self._directory_identity = directory_info.identity
        self._final_path = _windows_normalize_final_path(directory_info.final_path)
        self._operation_hook = operation_hook
        self._api = api
        self._closed = False

    @classmethod
    def open(
        cls,
        runtime_dir: Path,
        operation_hook: OperationHook | None,
        *,
        create: bool,
        api: _WindowsApi | None = None,
    ) -> _WindowsRuntimeDirectory:
        windows = api or _CtypesWindowsApi()
        safe_path = windows.validate_local_path(runtime_dir)
        if create:
            windows.create_directory(safe_path)
        handle = -1
        try:
            handle, info = windows.open_directory(safe_path)
            if info.is_reparse or not info.is_directory:
                raise StartupError("Windows 运行目录是 reparse point 或非目录对象")
            if not info.acl_safe:
                raise StartupError(
                    f"Windows 运行目录权限不是当前用户专用：{info.acl_detail}"
                )
            if _windows_normalize_final_path(info.final_path) != _windows_normalize_final_path(
                str(safe_path)
            ):
                raise StartupError("Windows 运行目录路径已被重定向")
            directory = cls(safe_path, handle, info, operation_hook, windows)
            handle = -1
            directory.verify_identity()
            return directory
        finally:
            if handle >= 0:
                windows.close(handle)

    def _hook(self, stage: str) -> None:
        if self._operation_hook is not None:
            self._operation_hook(stage)

    def verify_identity(self) -> None:
        opened = self._api.info(self._directory_handle)
        named_handle = -1
        try:
            named_handle, named = self._api.open_directory(self.path)
            if (
                opened.identity != self._directory_identity
                or named.identity != self._directory_identity
                or opened.is_reparse
                or named.is_reparse
                or not opened.is_directory
                or not named.is_directory
            ):
                raise StartupError("运行目录已被替换")
            if not opened.acl_safe or not named.acl_safe:
                raise StartupError(
                    "Windows 运行目录权限不是当前用户专用："
                    f"{opened.acl_detail or named.acl_detail}"
                )
            if (
                _windows_normalize_final_path(opened.final_path) != self._final_path
                or _windows_normalize_final_path(named.final_path) != self._final_path
            ):
                raise StartupError("运行目录已被替换")
        except StartupError:
            raise
        except OSError as exc:
            raise StartupError("运行目录已被替换") from exc
        finally:
            if named_handle >= 0:
                self._api.close(named_handle)

    def _validate_child(self, info: _WindowsFileInfo, label: str) -> None:
        if info.is_reparse:
            raise _UnsafeInstanceFile(f"{label} must not be a reparse point")
        if info.is_directory:
            raise _UnsafeInstanceFile(f"{label} must not be a directory")
        if info.link_count != 1:
            raise _UnsafeInstanceFile(f"{label} must not be a hard link")
        if not info.acl_safe:
            raise _UnsafeInstanceFile(f"{label}权限不是当前用户专用")
        child_parent = _windows_normalize_final_path(ntpath.dirname(info.final_path))
        if child_parent != self._final_path:
            raise _UnsafeInstanceFile(f"{label}不属于同一运行目录")

    def _open_child(
        self,
        name: str,
        *,
        access: str,
        creation: str,
        label: str,
        hook_stage: str | None = None,
    ) -> tuple[int, _WindowsFileInfo]:
        if hook_stage is not None:
            self._hook(hook_stage)
        self.verify_identity()
        handle = -1
        try:
            handle, info = self._api.open_file(self.path / name, access=access, creation=creation)
            self._validate_child(info, label)
            return handle, info
        except Exception:
            if handle >= 0:
                self._api.close(handle)
            raise

    def read_record(self) -> tuple[InstanceRecord, FileIdentity]:
        handle, info = self._open_child(
            RECORD_NAME,
            access="read",
            creation="open_existing",
            label="instance record",
            hook_stage="before_record_open",
        )
        try:
            if info.size > MAX_RECORD_BYTES:
                raise InvalidInstanceRecord("instance record is too large")
            raw = self._api.read(handle, MAX_RECORD_BYTES + 1)
            if len(raw) > MAX_RECORD_BYTES:
                raise InvalidInstanceRecord("instance record is too large")
            return _parse_record(raw), info.identity
        finally:
            self._api.close(handle)

    def _existing_record_is_safe(self) -> None:
        try:
            handle, _ = self._open_child(
                RECORD_NAME, access="read", creation="open_existing", label="instance record"
            )
        except FileNotFoundError:
            return
        self._api.close(handle)

    def write_record(self, record: InstanceRecord) -> FileIdentity:
        self._existing_record_is_safe()
        payload = json.dumps(asdict(record), ensure_ascii=True, separators=(",", ":")).encode()
        temporary_name = f".instance-{secrets.token_hex(16)}"
        temporary_path = self.path / temporary_name
        handle = -1
        published = False
        temporary_identity: FileIdentity | None = None
        try:
            handle, temporary_info = self._open_child(
                temporary_name,
                access="write",
                creation="create_new",
                label="temporary instance record",
            )
            temporary_identity = temporary_info.identity
            self._api.write(handle, payload)
            self._api.flush(handle)
            self._hook("before_record_publish")
            self.verify_identity()
            current = self._api.info(handle)
            self._validate_child(current, "temporary instance record")
            if current.identity != temporary_identity:
                raise StartupError("临时实例记录在发布时被替换")
            source_handle, source_info = self._open_child(
                temporary_name,
                access="read",
                creation="open_existing",
                label="temporary instance record",
            )
            self._api.close(source_handle)
            if source_info.identity != temporary_identity:
                raise StartupError("临时实例记录在发布时被替换")
            self._api.close(handle)
            handle = -1
            self._api.replace(temporary_path, self.path / RECORD_NAME)
            published = True
            self.verify_identity()
            final_handle, final_info = self._open_child(
                RECORD_NAME, access="read", creation="open_existing", label="instance record"
            )
            self._api.close(final_handle)
            if final_info.identity != temporary_identity:
                raise StartupError("实例记录在发布时被替换")
            return final_info.identity
        finally:
            if handle >= 0:
                self._api.close(handle)
            if not published and temporary_identity is not None:
                try:
                    self._api.delete(temporary_path, temporary_identity, self._final_path)
                except (FileNotFoundError, OSError, StartupError):
                    pass

    def record_exists(self) -> bool:
        self.verify_identity()
        try:
            handle, _ = self._open_child(
                RECORD_NAME, access="read", creation="open_existing", label="instance record"
            )
        except FileNotFoundError:
            return False
        self._api.close(handle)
        return True

    def remove_record(self, expected: FileIdentity | None = None) -> None:
        handle, info = self._open_child(
            RECORD_NAME, access="read", creation="open_existing", label="instance record"
        )
        self._api.close(handle)
        if expected is not None and info.identity != expected:
            return
        self._hook("before_record_cleanup")
        self.verify_identity()
        check_handle, check = self._open_child(
            RECORD_NAME, access="read", creation="open_existing", label="instance record"
        )
        self._api.close(check_handle)
        if check.identity != info.identity:
            raise StartupError("实例记录在清理时被替换")
        self._api.delete(self.path / RECORD_NAME, info.identity, self._final_path)
        self.verify_identity()

    def open_lock(self) -> int:
        try:
            handle, _ = self._open_child(
                LOCK_NAME,
                access="lock",
                creation="open_always",
                label="实例锁文件",
                hook_stage="before_lock_open",
            )
            return handle
        except InvalidInstanceRecord as exc:
            raise StartupError("实例锁文件不可信") from exc

    def lock(self, handle: int) -> bool:
        return self._api.lock(handle)

    def unlock(self, handle: int) -> None:
        self._api.unlock(handle)

    def close_lock(self, handle: int) -> None:
        self._api.close(handle)

    def close(self) -> None:
        if self._closed:
            return
        self._api.close(self._directory_handle)
        self._closed = True


class _WinByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_ulong),
        ("creation_time_low", ctypes.c_ulong),
        ("creation_time_high", ctypes.c_ulong),
        ("access_time_low", ctypes.c_ulong),
        ("access_time_high", ctypes.c_ulong),
        ("write_time_low", ctypes.c_ulong),
        ("write_time_high", ctypes.c_ulong),
        ("volume_serial", ctypes.c_ulong),
        ("size_high", ctypes.c_ulong),
        ("size_low", ctypes.c_ulong),
        ("link_count", ctypes.c_ulong),
        ("file_index_high", ctypes.c_ulong),
        ("file_index_low", ctypes.c_ulong),
    ]


class _WinFileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("attributes", ctypes.c_ulong), ("reparse_tag", ctypes.c_ulong)]


class _WinOverlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_void_p),
        ("internal_high", ctypes.c_void_p),
        ("offset", ctypes.c_ulong),
        ("offset_high", ctypes.c_ulong),
        ("event", ctypes.c_void_p),
    ]


class _WinSecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ulong),
        ("security_descriptor", ctypes.c_void_p),
        ("inherit_handle", ctypes.c_int),
    ]


class _WinFileDispositionInfoEx(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_ulong)]


class _CtypesWindowsApi:
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_ATTRIBUTE_TAG_INFO = 9
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_READ_ATTRIBUTES = 0x80
    _FILE_LIST_DIRECTORY = 0x1
    _READ_CONTROL = 0x00020000
    _DELETE = 0x00010000
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x1
    _FILE_SHARE_WRITE = 0x2
    _FILE_SHARE_DELETE = 0x4
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _MOVEFILE_REPLACE_EXISTING = 0x1
    _MOVEFILE_WRITE_THROUGH = 0x8
    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_SHARING_VIOLATION = 32
    _ERROR_ALREADY_EXISTS = 183
    _FILE_DISPOSITION_INFO_EX = 21
    _FILE_DISPOSITION_FLAG_DELETE = 1
    _DRIVE_FIXED = 3
    _INVALID_HANDLE_VALUE = int(ctypes.c_void_p(-1).value or -1)
    _OWNER_SECURITY_INFORMATION = 0x1
    _DACL_SECURITY_INFORMATION = 0x4
    _SDDL_REVISION_1 = 1

    # Declared, not inferred: on a non-Windows platform mypy proves the guard
    # in __init__ always raises, so the assignments below it are unreachable
    # and these attributes would have no type at all.
    _kernel32: Any
    _advapi32: Any
    _security_descriptor: ctypes.c_void_p
    _expected_sddl: str

    def __init__(self) -> None:
        if sys.platform != "win32" or not hasattr(ctypes, "WinDLL"):
            raise StartupError("Windows 安全 API 不可用，已拒绝启动")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._configure_api()
        self._security_descriptor, self._expected_sddl = self._make_security_descriptor()

    def _configure_api(self) -> None:
        void_p = ctypes.c_void_p
        ulong_p = ctypes.POINTER(ctypes.c_ulong)
        self._kernel32.GetCurrentProcess.restype = void_p
        self._kernel32.CloseHandle.argtypes = [void_p]
        self._kernel32.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, void_p]
        self._kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            void_p,
        ]
        self._kernel32.CreateFileW.restype = void_p
        self._kernel32.GetFileInformationByHandle.argtypes = [void_p, void_p]
        self._kernel32.GetFileInformationByHandleEx.argtypes = [
            void_p,
            ctypes.c_int,
            void_p,
            ctypes.c_ulong,
        ]
        self._kernel32.GetFinalPathNameByHandleW.argtypes = [
            void_p,
            ctypes.c_wchar_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        self._kernel32.GetFinalPathNameByHandleW.restype = ctypes.c_ulong
        self._kernel32.ReadFile.argtypes = [void_p, void_p, ctypes.c_ulong, ulong_p, void_p]
        self._kernel32.WriteFile.argtypes = [void_p, void_p, ctypes.c_ulong, ulong_p, void_p]
        self._kernel32.SetFilePointerEx.argtypes = [
            void_p,
            ctypes.c_longlong,
            ctypes.POINTER(ctypes.c_longlong),
            ctypes.c_ulong,
        ]
        self._kernel32.SetEndOfFile.argtypes = [void_p]
        self._kernel32.FlushFileBuffers.argtypes = [void_p]
        self._kernel32.MoveFileExW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_ulong,
        ]
        self._kernel32.SetFileInformationByHandle.argtypes = [
            void_p,
            ctypes.c_int,
            void_p,
            ctypes.c_ulong,
        ]
        self._kernel32.LockFileEx.argtypes = [
            void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            void_p,
        ]
        self._kernel32.UnlockFileEx.argtypes = [
            void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            void_p,
        ]
        self._kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        self._kernel32.GetDriveTypeW.restype = ctypes.c_uint
        self._kernel32.LocalFree.argtypes = [void_p]
        self._kernel32.LocalFree.restype = void_p
        self._advapi32.OpenProcessToken.argtypes = [void_p, ctypes.c_ulong, void_p]
        self._advapi32.GetTokenInformation.argtypes = [
            void_p,
            ctypes.c_int,
            void_p,
            ctypes.c_ulong,
            ulong_p,
        ]
        self._advapi32.ConvertSidToStringSidW.argtypes = [void_p, void_p]
        self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_ulong,
            void_p,
            ulong_p,
        ]
        self._advapi32.GetKernelObjectSecurity.argtypes = [
            void_p,
            ctypes.c_ulong,
            void_p,
            ctypes.c_ulong,
            ulong_p,
        ]
        self._advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            void_p,
            ulong_p,
        ]

    @staticmethod
    def _value(handle: object) -> int:
        value = getattr(handle, "value", handle)
        return int(cast(int, value)) if value is not None else -1

    @staticmethod
    def _error(context: str, code: int | None = None) -> OSError:
        number = ctypes.get_last_error() if code is None else code  # type: ignore[attr-defined, unused-ignore]
        error = cast(OSError, ctypes.WinError(number))  # type: ignore[attr-defined, redundant-cast, unused-ignore]
        error.args = (number, f"{context}: {error.strerror}")
        return error

    @staticmethod
    def _long_path(path: Path) -> str:
        absolute = ntpath.abspath(str(path))
        if absolute.startswith("\\\\?\\"):
            return absolute
        if absolute.startswith("\\\\"):
            return "\\\\?\\UNC\\" + absolute[2:]
        return "\\\\?\\" + absolute

    def _current_user_sid(self) -> str:
        token = ctypes.c_void_p()
        if not self._advapi32.OpenProcessToken(
            self._kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
        ):
            raise self._error("OpenProcessToken failed")
        try:
            needed = ctypes.c_ulong()
            self._advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(needed.value)
            if not self._advapi32.GetTokenInformation(
                token, 1, buffer, needed, ctypes.byref(needed)
            ):
                raise self._error("GetTokenInformation failed")
            sid_pointer = ctypes.c_void_p.from_buffer(buffer).value
            sid_text = ctypes.c_wchar_p()
            if not self._advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
                raise self._error("ConvertSidToStringSidW failed")
            try:
                if sid_text.value is None:
                    raise StartupError("无法确定当前 Windows 用户 SID")
                return sid_text.value
            finally:
                self._kernel32.LocalFree(sid_text)
        finally:
            self.close(self._value(token))

    def _make_security_descriptor(self) -> tuple[ctypes.c_void_p, str]:
        sid = self._current_user_sid()
        sddl = f"O:{sid}D:P(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)"
        descriptor = ctypes.c_void_p()
        if not self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, self._SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            raise self._error("ConvertStringSecurityDescriptorToSecurityDescriptorW failed")
        # Render the expectation through the same converter used to read a
        # descriptor back. Windows abbreviates well-known SIDs — the built-in
        # Administrator (RID 500) comes back as "LA" — so comparing against the
        # hand-built string rejects a directory whose ACL is in fact identical.
        return descriptor, self._render_descriptor(descriptor)

    def _render_descriptor(self, descriptor: ctypes.c_void_p) -> str:
        requested = self._OWNER_SECURITY_INFORMATION | self._DACL_SECURITY_INFORMATION
        text = ctypes.c_wchar_p()
        if not self._advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, self._SDDL_REVISION_1, requested, ctypes.byref(text), None
        ):
            raise self._error("ConvertSecurityDescriptorToStringSecurityDescriptorW failed")
        try:
            return text.value or ""
        finally:
            self._kernel32.LocalFree(text)

    def __del__(self) -> None:
        descriptor = getattr(self, "_security_descriptor", None)
        if descriptor:
            self._kernel32.LocalFree(descriptor)

    def _security_attributes(self) -> _WinSecurityAttributes:
        return _WinSecurityAttributes(
            ctypes.sizeof(_WinSecurityAttributes), self._security_descriptor, 0
        )

    def _acl_safe(self, handle: int) -> tuple[bool, str]:
        requested = self._OWNER_SECURITY_INFORMATION | self._DACL_SECURITY_INFORMATION
        needed = ctypes.c_ulong()
        self._advapi32.GetKernelObjectSecurity(handle, requested, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise self._error("GetKernelObjectSecurity failed")
        buffer = ctypes.create_string_buffer(needed.value)
        if not self._advapi32.GetKernelObjectSecurity(
            handle, requested, buffer, needed, ctypes.byref(needed)
        ):
            raise self._error("GetKernelObjectSecurity failed")
        text = ctypes.c_wchar_p()
        if not self._advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            buffer, self._SDDL_REVISION_1, requested, ctypes.byref(text), None
        ):
            raise self._error("ConvertSecurityDescriptorToStringSecurityDescriptorW failed")
        try:
            rendered = text.value or ""
            expected_owner, expected_dacl = self._expected_sddl.split("D:P", 1)
            detail = f"实际 {rendered} / 期望 {self._expected_sddl}"
            if not rendered.startswith(expected_owner) or "D:P" not in rendered:
                return False, detail
            rendered_dacl = rendered.split("D:P", 1)[1]
            matched = sorted(re.findall(r"\([^)]*\)", rendered_dacl)) == sorted(
                re.findall(r"\([^)]*\)", expected_dacl)
            )
            return matched, "" if matched else detail
        finally:
            self._kernel32.LocalFree(text)

    def validate_local_path(self, path: Path) -> Path:
        raw = str(path)
        if raw.startswith(("\\\\", "//")):
            raise StartupError("Windows 运行目录必须位于本机固定磁盘，不能使用 UNC/network 路径")
        absolute = Path(ntpath.abspath(raw))
        drive = ntpath.splitdrive(str(absolute))[0]
        if not drive or self._kernel32.GetDriveTypeW(f"{drive}\\") != self._DRIVE_FIXED:
            raise StartupError("Windows 运行目录必须位于本机固定磁盘")
        # Open every existing ancestor without following its reparse point.
        parts = PureWindowsPath(str(absolute)).parts
        current = Path(parts[0])
        for component in parts[1:-1]:
            current /= component
            try:
                handle, info = self.open_directory(current)
            except FileNotFoundError:
                continue
            try:
                if info.is_reparse:
                    raise StartupError("Windows 运行目录路径包含 reparse point/junction")
            finally:
                self.close(handle)
        return absolute

    def create_directory(self, path: Path) -> None:
        parts = PureWindowsPath(str(path)).parts
        current = Path(parts[0])
        attributes = self._security_attributes()
        for component in parts[1:]:
            current /= component
            created = bool(
                self._kernel32.CreateDirectoryW(self._long_path(current), ctypes.byref(attributes))
            )
            if not created:
                code = ctypes.get_last_error()  # type: ignore[attr-defined, unused-ignore]
                if code != self._ERROR_ALREADY_EXISTS:
                    raise self._error(f"CreateDirectoryW failed for {current}", code)
            handle, info = self.open_directory(current)
            try:
                if info.is_reparse or not info.is_directory:
                    raise StartupError("Windows 运行目录路径包含 reparse point/junction")
                if created and not info.acl_safe:
                    raise StartupError(
                        f"新建 Windows 运行目录权限不是当前用户专用：{info.acl_detail}"
                    )
            finally:
                self.close(handle)

    def _create_file(
        self,
        path: Path,
        access: int,
        creation: int,
        *,
        directory: bool,
        share_delete: bool,
    ) -> tuple[int, _WindowsFileInfo]:
        attributes = self._security_attributes()
        flags = self._FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= self._FILE_FLAG_BACKUP_SEMANTICS
        ctypes.set_last_error(0)  # type: ignore[attr-defined, unused-ignore]
        share_mode = self._FILE_SHARE_READ | self._FILE_SHARE_WRITE
        if share_delete:
            share_mode |= self._FILE_SHARE_DELETE
        raw_handle = self._kernel32.CreateFileW(
            self._long_path(path),
            access | self._FILE_READ_ATTRIBUTES | self._READ_CONTROL,
            share_mode,
            ctypes.byref(attributes),
            creation,
            flags,
            None,
        )
        handle = self._value(raw_handle)
        if handle in {-1, self._INVALID_HANDLE_VALUE}:
            code = ctypes.get_last_error()  # type: ignore[attr-defined, unused-ignore]
            if code in {2, 3}:
                raise FileNotFoundError(path)
            if code == 80:
                raise FileExistsError(path)
            raise self._error(f"CreateFileW failed for {path}", code)
        try:
            return handle, self.info(handle)
        except Exception:
            self.close(handle)
            raise

    def open_directory(self, path: Path) -> tuple[int, _WindowsFileInfo]:
        return self._create_file(
            path,
            self._FILE_LIST_DIRECTORY,
            self._OPEN_EXISTING,
            directory=True,
            share_delete=False,
        )

    def open_file(self, path: Path, *, access: str, creation: str) -> tuple[int, _WindowsFileInfo]:
        desired = {
            "read": self._GENERIC_READ,
            "write": self._GENERIC_WRITE | self._GENERIC_READ,
            "lock": self._GENERIC_READ | self._GENERIC_WRITE,
        }[access]
        disposition = {
            "open_existing": self._OPEN_EXISTING,
            "create_new": self._CREATE_NEW,
            "open_always": self._OPEN_ALWAYS,
        }[creation]
        return self._create_file(
            path, desired, disposition, directory=False, share_delete=True
        )

    def info(self, handle: int) -> _WindowsFileInfo:
        basic = _WinByHandleFileInformation()
        if not self._kernel32.GetFileInformationByHandle(handle, ctypes.byref(basic)):
            raise self._error("GetFileInformationByHandle failed")
        tag = _WinFileAttributeTagInfo()
        if not self._kernel32.GetFileInformationByHandleEx(
            handle, self._FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(tag), ctypes.sizeof(tag)
        ):
            raise self._error("GetFileInformationByHandleEx failed")
        needed = self._kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not needed:
            raise self._error("GetFinalPathNameByHandleW failed")
        final_buffer = ctypes.create_unicode_buffer(needed + 1)
        if not self._kernel32.GetFinalPathNameByHandleW(handle, final_buffer, len(final_buffer), 0):
            raise self._error("GetFinalPathNameByHandleW failed")
        acl_safe, acl_detail = self._acl_safe(handle)
        return _WindowsFileInfo(
            identity=(
                int(basic.volume_serial),
                (int(basic.file_index_high) << 32) | int(basic.file_index_low),
            ),
            final_path=final_buffer.value,
            is_directory=bool(tag.attributes & self._FILE_ATTRIBUTE_DIRECTORY),
            is_reparse=bool(tag.attributes & self._FILE_ATTRIBUTE_REPARSE_POINT),
            link_count=int(basic.link_count),
            size=(int(basic.size_high) << 32) | int(basic.size_low),
            acl_safe=acl_safe,
            acl_detail=acl_detail,
        )

    def read(self, handle: int, limit: int) -> bytes:
        buffer = ctypes.create_string_buffer(limit)
        read = ctypes.c_ulong()
        if not self._kernel32.ReadFile(handle, buffer, limit, ctypes.byref(read), None):
            raise self._error("ReadFile failed")
        return bytes(buffer.raw[: read.value])

    def write(self, handle: int, payload: bytes) -> None:
        position = ctypes.c_longlong()
        if not self._kernel32.SetFilePointerEx(handle, 0, ctypes.byref(position), 0):
            raise self._error("SetFilePointerEx failed")
        if not self._kernel32.SetEndOfFile(handle):
            raise self._error("SetEndOfFile failed")
        offset = 0
        while offset < len(payload):
            written = ctypes.c_ulong()
            chunk = payload[offset:]
            buffer = ctypes.create_string_buffer(chunk)
            if not self._kernel32.WriteFile(
                handle, buffer, len(chunk), ctypes.byref(written), None
            ):
                raise self._error("WriteFile failed")
            if written.value == 0:
                raise OSError("WriteFile returned a short write")
            offset += written.value

    def flush(self, handle: int) -> None:
        if not self._kernel32.FlushFileBuffers(handle):
            raise self._error("FlushFileBuffers failed")

    def replace(self, source: Path, target: Path) -> None:
        source_text, target_text = self._long_path(source), self._long_path(target)
        if not self._kernel32.MoveFileExW(
            source_text,
            target_text,
            self._MOVEFILE_REPLACE_EXISTING | self._MOVEFILE_WRITE_THROUGH,
        ):
            raise self._error("MoveFileExW failed")

    def delete(self, path: Path, expected: FileIdentity, expected_parent: str) -> None:
        handle, info = self._create_file(
            path,
            self._DELETE | self._GENERIC_READ,
            self._OPEN_EXISTING,
            directory=False,
            share_delete=True,
        )
        try:
            if (
                info.identity != expected
                or info.is_reparse
                or info.is_directory
                or info.link_count != 1
                or _windows_normalize_final_path(ntpath.dirname(info.final_path)) != expected_parent
            ):
                raise StartupError("实例记录在清理时被替换")
            disposition = _WinFileDispositionInfoEx(self._FILE_DISPOSITION_FLAG_DELETE)
            if not self._kernel32.SetFileInformationByHandle(
                handle,
                self._FILE_DISPOSITION_INFO_EX,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            ):
                raise self._error("SetFileInformationByHandle delete failed")
        finally:
            self.close(handle)

    def lock(self, handle: int) -> bool:
        overlapped = _WinOverlapped()
        if self._kernel32.LockFileEx(
            handle,
            self._LOCKFILE_EXCLUSIVE_LOCK | self._LOCKFILE_FAIL_IMMEDIATELY,
            0,
            1,
            0,
            ctypes.byref(overlapped),
        ):
            return True
        code = ctypes.get_last_error()  # type: ignore[attr-defined, unused-ignore]
        if code in {self._ERROR_LOCK_VIOLATION, self._ERROR_SHARING_VIOLATION}:
            return False
        raise self._error("LockFileEx failed", code)

    def unlock(self, handle: int) -> None:
        overlapped = _WinOverlapped()
        if not self._kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
            raise self._error("UnlockFileEx failed")

    def close(self, handle: int) -> None:
        if not self._kernel32.CloseHandle(handle):
            raise self._error("CloseHandle failed")


def _open_runtime_directory(
    runtime_dir: Path, operation_hook: OperationHook | None, *, create: bool
) -> _PosixRuntimeDirectory | _WindowsRuntimeDirectory:
    if sys.platform == "win32":
        return _WindowsRuntimeDirectory.open(runtime_dir, operation_hook, create=create)
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


def runtime_identity(runtime_dir: Path | str) -> str:
    """Fingerprint the data root an instance serves.

    Two instances started against different data roots compete for the same
    port range, so a healthy FIS answering on a recorded port is not
    necessarily the one that wrote the record. The fingerprint lets a probe
    tell them apart. It is a digest rather than the path itself so that
    nothing on the wire describes the user's filesystem.
    """

    resolved = os.path.normcase(os.path.abspath(str(runtime_dir)))
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:32]


def probe_fis(port: int, *, identity: str | None = None, timeout: float = 0.35) -> bool:
    if not 8765 <= port <= 8775:
        return False
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            body = response.read(MAX_RECORD_BYTES + 1)
            if (
                cast(int, response.status) != 200
                or response.headers.get("X-FIS-Product") != PRODUCT_MARKER
                or len(body) > MAX_RECORD_BYTES
            ):
                return False
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                return False
            if identity is None:
                return True
            # A build that cannot name its data root is not provably ours.
            return payload.get("instance") == identity
    except (OSError, ValueError, json.JSONDecodeError):
        return False


class InstanceLease:
    def __init__(
        self,
        directory: _PosixRuntimeDirectory | _WindowsRuntimeDirectory,
        lock_descriptor: int,
    ) -> None:
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
        try:
            self._directory.unlock(self._lock_descriptor)
        finally:
            try:
                self._directory.close_lock(self._lock_descriptor)
            finally:
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
    if sys.platform == "win32":
        return _acquire_windows_instance(runtime_dir, probe=probe, _operation_hook=_operation_hook)
    directory = _open_runtime_directory(runtime_dir, _operation_hook, create=True)
    lock_descriptor = -1
    keep_resources = False
    try:
        lock_descriptor = directory.open_lock()
        if not directory.lock(lock_descriptor):
            try:
                record, _ = directory.read_record()
            except (FileNotFoundError, InvalidInstanceRecord) as record_exc:
                raise StartupError("另一个实例持有锁，但无法确认其健康状态") from record_exc
            if probe(record.port):
                return ExistingInstance(record.port)
            raise StartupError("另一个实例持有锁，但无法确认其为健康的 FIS 实例")

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
                directory.close_lock(lock_descriptor)
            if not directory._closed:
                directory.close()


def _acquire_windows_instance(
    runtime_dir: Path,
    *,
    probe: Probe,
    _operation_hook: OperationHook | None = None,
    api: _WindowsApi | None = None,
) -> InstanceLease | ExistingInstance:
    directory = _WindowsRuntimeDirectory.open(runtime_dir, _operation_hook, create=True, api=api)
    lock_handle = -1
    keep_resources = False
    try:
        lock_handle = directory.open_lock()
        if not directory.lock(lock_handle):
            try:
                record, _ = directory.read_record()
            except (FileNotFoundError, InvalidInstanceRecord) as exc:
                raise StartupError("另一个实例持有锁，但无法确认其健康状态") from exc
            if probe(record.port):
                return ExistingInstance(record.port)
            raise StartupError("另一个实例持有锁，但无法确认其为健康的 FIS 实例")

        lease = InstanceLease(directory, lock_handle)
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
                    lock_handle = -1
                    return ExistingInstance(record.port)
                directory.remove_record(identity)
        keep_resources = True
        return lease
    finally:
        if not keep_resources:
            if lock_handle >= 0:
                directory.close_lock(lock_handle)
            if not directory._closed:
                directory.close()
