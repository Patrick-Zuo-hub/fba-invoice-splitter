from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Where FIS keeps its data.

    ``work_root`` holds batch working copies, which are per-upload scratch. A
    packaged build points it at a machine-local directory so the portable
    folder stays small; leaving it unset keeps everything in one place, which
    is what development and the tests want.
    """

    data_root: Path
    _work_root: Path | None = None

    def __init__(self, data_root: Path, work_root: Path | None = None) -> None:
        object.__setattr__(self, "data_root", data_root)
        object.__setattr__(self, "_work_root", work_root)

    @property
    def work_root(self) -> Path:
        return self._work_root if self._work_root is not None else self.data_root

    @property
    def database_path(self) -> Path:
        return self.data_root / "fis.db"

    @property
    def batches_root(self) -> Path:
        return self.work_root / "batches"
