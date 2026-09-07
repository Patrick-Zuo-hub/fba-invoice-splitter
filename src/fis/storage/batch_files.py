import errno
import os
import re
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_SAFE_BATCH_ID = re.compile(
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|batch-[a-z0-9-]+)"
)
_USE_DIRECTORY_FDS = (
    os.name != "nt"
    and shutil.rmtree.avoids_symlink_attacks
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)


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
        else:
            self._require_secure_directory_handles()


__all__ = ["BatchFiles"]
