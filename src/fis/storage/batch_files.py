import re
import shutil
from pathlib import Path

_SAFE_BATCH_ID = re.compile(
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|batch-[a-z0-9-]+)"
)


class BatchFiles:
    """Resolve and manage the filesystem directories belonging to batches."""

    def __init__(self, data_root: Path) -> None:
        self.batches_root = data_root.resolve() / "batches"

    def _batch(self, batch_id: str) -> Path:
        if not isinstance(batch_id, str) or _SAFE_BATCH_ID.fullmatch(batch_id) is None:
            raise ValueError("Invalid batch ID")
        if self.batches_root.is_symlink():
            raise ValueError("Symlinked batches directory is not allowed")
        batch = self.batches_root / batch_id
        if batch.is_symlink():
            raise ValueError("Symlinked batch directory is not allowed")
        return batch

    def work_path(self, batch_id: str) -> Path:
        return self._batch(batch_id) / "work"

    def output_path(self, batch_id: str) -> Path:
        return self._batch(batch_id) / "output"

    def create(self, batch_id: str) -> Path:
        work = self._batch(batch_id) / "work"
        work.mkdir(parents=True, exist_ok=False)
        return work

    def publish(self, batch_id: str) -> Path:
        batch = self._batch(batch_id)
        work = batch / "work"
        output = batch / "output"
        if work.is_symlink():
            raise ValueError("Symlinked batch work directory is not allowed")
        if not work.exists():
            raise FileNotFoundError(work)
        if not work.is_dir():
            raise NotADirectoryError(work)
        if output.is_symlink() or output.exists():
            raise FileExistsError("Batch output already exists")
        work.replace(output)
        return output

    @staticmethod
    def _clear_directory(path: Path, label: str) -> None:
        if path.is_symlink():
            raise ValueError(f"Symlinked batch {label} directory is not allowed")
        if not path.exists():
            return
        if not path.is_dir():
            raise NotADirectoryError(path)
        shutil.rmtree(path)

    def clear_work(self, batch_id: str) -> None:
        self._clear_directory(self.work_path(batch_id), "work")

    def clear_output(self, batch_id: str) -> None:
        self._clear_directory(self.output_path(batch_id), "output")

    def delete_batch(self, batch_id: str) -> None:
        batch = self._batch(batch_id)
        if batch.is_symlink():
            raise ValueError("Symlinked batch directory is not allowed")
        if not batch.exists():
            return
        if not batch.is_dir():
            raise NotADirectoryError(batch)
        shutil.rmtree(batch)


__all__ = ["BatchFiles"]
