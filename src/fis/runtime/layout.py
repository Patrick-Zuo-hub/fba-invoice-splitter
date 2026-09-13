"""Decide where the durable and the transient data live."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fis.config import Settings
from fis.runtime.instance import StartupError

_DEVELOPMENT_DIRECTORY = ".fba-invoice-splitter"
_PORTABLE_DATA_DIRECTORY = "data"
_MACHINE_DIRECTORY = "FBA-Invoice-Splitter"


def _machine_work_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / _MACHINE_DIRECTORY
    return Path.home() / _MACHINE_DIRECTORY


def _require_writable(data_root: Path) -> None:
    """Refuse rather than relocate when the portable folder is read-only.

    Falling back to the home directory would look like it worked, and the user
    would find out only after copying the folder to another machine and
    discovering their master data never travelled with it.
    """

    probe = data_root if data_root.exists() else data_root.parent
    if not os.access(probe, os.W_OK):
        raise StartupError(
            f"无法写入数据目录 {data_root}。"
            "请把 FIS 文件夹放在桌面、D 盘或用户目录下，不要放在 Program Files 中。"
        )


def resolve_layout(*, frozen: bool | None = None) -> Settings:
    """Locate the data root, and the machine-local root for batch working files.

    A packaged build keeps ``fis.db`` and ``runtime/`` beside the executable so
    that copying the folder carries the master data with it. Batch working
    copies stay on the machine: they are regenerated per upload and would
    otherwise drag hundreds of megabytes along on every copy.

    ``runtime/`` has to stay with ``fis.db``: the single-instance identity is a
    digest of the runtime directory, so two portable copies sharing one
    machine-level runtime directory would mistake each other for the same
    instance.
    """

    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))

    override = os.environ.get("FIS_DATA_ROOT")
    if override:
        data_root = Path(override).expanduser()
        return Settings(data_root=data_root, work_root=data_root)

    if not frozen:
        data_root = Path.home() / _DEVELOPMENT_DIRECTORY
        return Settings(data_root=data_root, work_root=data_root)

    data_root = Path(sys.executable).resolve().parent / _PORTABLE_DATA_DIRECTORY
    _require_writable(data_root)
    return Settings(data_root=data_root, work_root=_machine_work_root())


__all__ = ["StartupError", "resolve_layout"]
