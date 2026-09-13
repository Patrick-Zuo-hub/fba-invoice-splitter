import ctypes
import errno
import os
import re
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

_SAFE_BATCH_ID = re.compile(
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|batch-[a-z0-9-]+)"
)
_USE_DIRECTORY_FDS = (
    os.name != "nt"
    and shutil.rmtree.avoids_symlink_attacks
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)

_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_TAG_INFO = 9
_FILE_DISPOSITION_INFO = 4
_FILE_DISPOSITION_INFO_EX = 21
_FILE_RENAME_INFO = 3
_FILE_DISPOSITION_FLAG_DELETE = 1
_FILE_LIST_DIRECTORY = 0x1
_DELETE = 0x10000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_READ_ATTRIBUTES = 0x80
_FILE_SHARE_READ = 0x1
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = int(ctypes.c_void_p(-1).value or -1)


class _WindowsFileAttributeTagInfo(ctypes.Structure):
    _fields_ = [
        ("file_attributes", ctypes.c_ulong),
        ("reparse_tag", ctypes.c_ulong),
    ]


class _WindowsFileDispositionInfo(ctypes.Structure):
    _fields_ = [("delete_file", ctypes.c_ubyte)]


class _WindowsFileDispositionInfoEx(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_ulong)]


@lru_cache(maxsize=1)
def _windows_api() -> tuple[Any, Any, Any, Any]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p

    get_file_info = kernel32.GetFileInformationByHandleEx
    get_file_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    get_file_info.restype = ctypes.c_int

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    set_file_info = kernel32.SetFileInformationByHandle
    set_file_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    set_file_info.restype = ctypes.c_int

    return create_file, get_file_info, close_handle, set_file_info


def _windows_error(error_code: int, context: str) -> OSError:
    error = cast(OSError, ctypes.WinError(error_code))  # type: ignore[attr-defined]
    error.args = (error_code, f"{context}: {error.strerror}")
    return error


def _windows_close_handle(handle: int) -> None:
    _, _, close_handle, _ = _windows_api()
    close_handle(handle)


def _windows_open_handle(path: Path, *, require_directory: bool, delete: bool = False) -> int:
    create_file, get_file_info, _, _ = _windows_api()
    desired_access = _FILE_READ_ATTRIBUTES
    if require_directory:
        desired_access |= _FILE_LIST_DIRECTORY
    if delete:
        desired_access |= _DELETE
    handle = create_file(
        str(path),
        desired_access,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    handle_value = getattr(handle, "value", handle)
    if handle_value is None or int(handle_value) in {-1, _INVALID_HANDLE_VALUE}:
        error_code = ctypes.get_last_error()  # type: ignore[attr-defined]
        raise _windows_error(error_code, f"CreateFileW failed for {path}")

    handle_int = int(handle_value)
    info = _WindowsFileAttributeTagInfo()
    if not get_file_info(
        handle_int,
        _FILE_ATTRIBUTE_TAG_INFO,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error_code = ctypes.get_last_error()  # type: ignore[attr-defined]
        _windows_close_handle(handle_int)
        raise _windows_error(error_code, f"GetFileInformationByHandleEx failed for {path}")
    if info.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        _windows_close_handle(handle_int)
        raise ValueError(f"Symlinked or junction directory is not allowed: {path}")
    if require_directory and not info.file_attributes & _FILE_ATTRIBUTE_DIRECTORY:
        _windows_close_handle(handle_int)
        raise NotADirectoryError(path)
    return handle_int


def _windows_file_attributes(handle: int, path: Path) -> int:
    _, get_file_info, _, _ = _windows_api()
    info = _WindowsFileAttributeTagInfo()
    if not get_file_info(
        handle,
        _FILE_ATTRIBUTE_TAG_INFO,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error_code = ctypes.get_last_error()  # type: ignore[attr-defined]
        raise _windows_error(error_code, f"GetFileInformationByHandleEx failed for {path}")
    return int(info.file_attributes)


def _windows_open_directory(path: Path) -> int:
    return _windows_open_handle(path, require_directory=True)


def _windows_mark_delete(handle: int, path: Path) -> None:
    _, _, _, set_file_info = _windows_api()
    disposition_ex = _WindowsFileDispositionInfoEx(flags=_FILE_DISPOSITION_FLAG_DELETE)
    if set_file_info(
        handle,
        _FILE_DISPOSITION_INFO_EX,
        ctypes.byref(disposition_ex),
        ctypes.sizeof(disposition_ex),
    ):
        return
    disposition = _WindowsFileDispositionInfo(delete_file=1)
    if set_file_info(
        handle,
        _FILE_DISPOSITION_INFO,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        return
    error_code = ctypes.get_last_error()  # type: ignore[attr-defined]
    raise _windows_error(error_code, f"SetFileInformationByHandle failed for {path}")


def _windows_rename_directory(source: Path, destination: Path) -> None:
    """Rename through the source handle rather than through MoveFileEx.

    MoveFileEx has to open the parent directory for write, which the guard
    handle's read-only share mode refuses — that share mode is deliberate, and
    `test_windows_protected_directory_rejects_write_handle` pins it. Issuing
    FileRenameInfo on a handle we already hold touches no other path, so the
    tamper protection stays in force for the whole operation.
    """

    _, _, _, set_file_info = _windows_api()
    target = str(destination)
    buffer_length = len(target) + 1

    class _WindowsFileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("replace_if_exists", ctypes.c_ubyte),
            ("root_directory", ctypes.c_void_p),
            ("file_name_length", ctypes.c_ulong),
            ("file_name", ctypes.c_wchar * buffer_length),
        ]

    info = _WindowsFileRenameInfo()
    info.replace_if_exists = 0
    info.root_directory = None
    info.file_name_length = len(target) * 2  # bytes, excluding the terminator
    info.file_name = target

    handle = _windows_open_handle(source, require_directory=True, delete=True)
    try:
        if not set_file_info(
            handle, _FILE_RENAME_INFO, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error_code = ctypes.get_last_error()  # type: ignore[attr-defined]
            raise _windows_error(
                error_code, f"SetFileInformationByHandle rename failed for {source}"
            )
    finally:
        _windows_close_handle(handle)


def _windows_path_prefixes(path: Path) -> tuple[Path, ...]:
    parts = path.parts
    current = Path(parts[0])
    prefixes = [current]
    for part in parts[1:]:
        current /= part
        prefixes.append(current)
    return tuple(prefixes)


class BatchFiles:
    """Resolve and manage the filesystem directories belonging to batches."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()
        self.batches_root = self.data_root / "batches"

    @staticmethod
    def _is_redirect(path: Path) -> bool:
        return path.is_symlink() or path.is_junction()

    @classmethod
    def _validate_path(cls, path: Path, label: str) -> None:
        if cls._is_redirect(path):
            raise ValueError(f"Symlinked or junction {label} is not allowed")
        if path.resolve() != path:
            raise ValueError(f"Redirected {label} is not allowed")

    @staticmethod
    def _open_directory(path: str | Path, dir_fd: int | None = None) -> int:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            return os.open(path, flags, dir_fd=dir_fd)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ValueError("Symlinked or junction batch path is not allowed") from error
            raise

    @contextmanager
    def _trusted_data_root(self) -> Iterator[int]:
        current_fd = self._open_directory(os.sep)
        try:
            for component in self.data_root.parts[1:]:
                next_fd = self._open_directory(component, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            yield current_fd
        finally:
            os.close(current_fd)

    @contextmanager
    def _trusted_batch_parent(self) -> Iterator[int]:
        with self._trusted_data_root() as data_fd:
            batches_fd = self._open_directory("batches", dir_fd=data_fd)
            try:
                yield batches_fd
            finally:
                os.close(batches_fd)

    @contextmanager
    def _trusted_batch_directory(self, batch_id: str) -> Iterator[int]:
        with self._trusted_batch_parent() as batches_fd:
            batch_fd = self._open_directory(batch_id, dir_fd=batches_fd)
            try:
                yield batch_fd
            finally:
                os.close(batch_fd)

    @staticmethod
    def _require_secure_directory_handles() -> None:
        if not _USE_DIRECTORY_FDS:
            raise RuntimeError("secure directory handles are unavailable")

    @staticmethod
    def _child_stat(parent_fd: int, name: str, label: str) -> os.stat_result | None:
        try:
            child = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(child.st_mode):
            raise ValueError(f"Symlinked {label} is not allowed")
        return child

    @staticmethod
    def _remove_child(parent_fd: int, name: str, label: str) -> None:
        child = BatchFiles._child_stat(parent_fd, name, label)
        if child is None:
            return
        if not stat.S_ISDIR(child.st_mode):
            raise NotADirectoryError(name)
        try:
            shutil.rmtree(name, dir_fd=parent_fd)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ValueError(f"Symlinked {label} is not allowed") from error
            raise

    @contextmanager
    def _windows_data_handles(self) -> Iterator[list[int]]:
        handles: list[int] = []
        try:
            for prefix in _windows_path_prefixes(self.data_root):
                handles.append(_windows_open_directory(prefix))
            yield handles
        finally:
            for handle in reversed(handles):
                _windows_close_handle(handle)

    @contextmanager
    def _windows_batch_parent_handles(self) -> Iterator[int]:
        with self._windows_data_handles():
            batches_handle = _windows_open_directory(self.batches_root)
            try:
                yield batches_handle
            finally:
                _windows_close_handle(batches_handle)

    @contextmanager
    def _windows_batch_handles(self, batch_id: str) -> Iterator[int]:
        with self._windows_batch_parent_handles():
            batch_handle = _windows_open_directory(self.batches_root / batch_id)
            try:
                yield batch_handle
            finally:
                _windows_close_handle(batch_handle)

    @staticmethod
    def _windows_validate_directory(path: Path, label: str) -> None:
        handle = _windows_open_directory(path)
        _windows_close_handle(handle)

    @staticmethod
    def _windows_delete_tree(path: Path, label: str) -> None:
        handle = _windows_open_handle(path, require_directory=True, delete=True)
        try:
            BatchFiles._windows_delete_tree_from_handle(handle, path, label)
            _windows_mark_delete(handle, path)
        finally:
            _windows_close_handle(handle)

    @staticmethod
    def _windows_delete_tree_from_handle(handle: int, path: Path, label: str) -> None:
        for entry in os.scandir(path):
            child_path = path / entry.name
            child_handle = _windows_open_handle(child_path, require_directory=False, delete=True)
            try:
                attributes = _windows_file_attributes(child_handle, child_path)
                if attributes & _FILE_ATTRIBUTE_DIRECTORY:
                    BatchFiles._windows_delete_tree_from_handle(child_handle, child_path, label)
                _windows_mark_delete(child_handle, child_path)
            finally:
                _windows_close_handle(child_handle)

    def _windows_publish(self, batch_id: str, work: Path, output: Path) -> None:
        with self._windows_batch_handles(batch_id):
            self._windows_validate_directory(work, "batch work directory")
            if self._is_redirect(output) or output.exists():
                raise FileExistsError("Batch output already exists")
            _windows_rename_directory(work, output)

    def _windows_clear_directory(self, batch_id: str, path: Path, label: str) -> None:
        with self._windows_batch_handles(batch_id):
            self._windows_delete_tree(path, f"batch {label} directory")

    def _windows_delete_batch(self, batch_id: str, batch: Path) -> None:
        with self._windows_batch_parent_handles():
            self._windows_delete_tree(batch, "batch directory")

    def _batch(self, batch_id: str) -> Path:
        if not isinstance(batch_id, str) or _SAFE_BATCH_ID.fullmatch(batch_id) is None:
            raise ValueError("Invalid batch ID")
        self._validate_path(self.data_root, "data root")
        self._validate_path(self.batches_root, "batches directory")
        batch = self.batches_root / batch_id
        self._validate_path(batch, "batch directory")
        return batch

    def work_path(self, batch_id: str) -> Path:
        return self._batch(batch_id) / "work"

    def output_path(self, batch_id: str) -> Path:
        return self._batch(batch_id) / "output"

    def create(self, batch_id: str) -> Path:
        work = self._batch(batch_id) / "work"
        self._validate_path(work, "batch work directory")
        work.mkdir(parents=True, exist_ok=False)
        return work

    def publish(self, batch_id: str) -> Path:
        batch = self._batch(batch_id)
        work = batch / "work"
        output = batch / "output"
        self._validate_path(work, "batch work directory")
        if self._is_redirect(output):
            raise FileExistsError("Batch output already exists")
        self._validate_path(output, "batch output directory")
        if not work.exists():
            raise FileNotFoundError(work)
        if not work.is_dir():
            raise NotADirectoryError(work)
        if output.is_symlink() or output.exists():
            raise FileExistsError("Batch output already exists")
        if _USE_DIRECTORY_FDS:
            try:
                with self._trusted_batch_directory(batch_id) as batch_fd:
                    work_stat = self._child_stat(batch_fd, "work", "batch work directory")
                    if work_stat is None:
                        raise FileNotFoundError(work)
                    if not stat.S_ISDIR(work_stat.st_mode):
                        raise NotADirectoryError(work)
                    output_stat = self._child_stat(batch_fd, "output", "batch output directory")
                    if output_stat is not None:
                        raise FileExistsError("Batch output already exists")
                    os.rename("work", "output", src_dir_fd=batch_fd, dst_dir_fd=batch_fd)
            except FileNotFoundError as error:
                raise FileNotFoundError(work) from error
        elif os.name == "nt":
            self._windows_publish(batch_id, work, output)
        else:
            self._require_secure_directory_handles()
        return output

    def _clear_directory(self, path: Path, label: str) -> None:
        BatchFiles._validate_path(path, f"batch {label} directory")
        if not path.exists():
            return
        if not path.is_dir():
            raise NotADirectoryError(path)
        if _USE_DIRECTORY_FDS:
            try:
                with self._trusted_batch_directory(path.parent.name) as batch_fd:
                    BatchFiles._remove_child(batch_fd, path.name, f"batch {label} directory")
            except FileNotFoundError:
                return
        elif os.name == "nt":
            self._windows_clear_directory(path.parent.name, path, label)
        else:
            self._require_secure_directory_handles()

    def clear_work(self, batch_id: str) -> None:
        self._clear_directory(self.work_path(batch_id), "work")

    def clear_output(self, batch_id: str) -> None:
        self._clear_directory(self.output_path(batch_id), "output")

    def delete_batch(self, batch_id: str) -> None:
        batch = self._batch(batch_id)
        if not batch.exists():
            return
        if not batch.is_dir():
            raise NotADirectoryError(batch)
        if _USE_DIRECTORY_FDS:
            try:
                with self._trusted_batch_parent() as batches_fd:
                    self._remove_child(batches_fd, batch.name, "batch directory")
            except FileNotFoundError:
                return
        elif os.name == "nt":
            self._windows_delete_batch(batch_id, batch)
        else:
            self._require_secure_directory_handles()


__all__ = ["BatchFiles"]
