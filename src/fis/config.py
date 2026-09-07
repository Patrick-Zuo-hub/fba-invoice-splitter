from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_root: Path

    @property
    def database_path(self) -> Path:
        return self.data_root / "fis.db"

    @property
    def batches_root(self) -> Path:
        return self.data_root / "batches"
