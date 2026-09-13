from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fis.config import Settings
from fis.runtime.layout import StartupError, resolve_layout


def test_work_root_defaults_to_the_data_root(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "data")

    assert settings.work_root == tmp_path / "data"
    assert settings.batches_root == tmp_path / "data" / "batches"


def test_an_explicit_work_root_moves_only_the_batches(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "data", work_root=tmp_path / "scratch")

    assert settings.database_path == tmp_path / "data" / "fis.db"
    assert settings.batches_root == tmp_path / "scratch" / "batches"


def test_development_keeps_everything_under_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FIS_DATA_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    settings = resolve_layout(frozen=False)

    assert settings.data_root == tmp_path / ".fba-invoice-splitter"
    assert settings.work_root == settings.data_root


def test_a_frozen_build_keeps_data_beside_the_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the portable folder: copy it and the data comes along."""

    monkeypatch.delenv("FIS_DATA_ROOT", raising=False)
    program = tmp_path / "FIS"
    program.mkdir()
    monkeypatch.setattr(sys, "executable", str(program / "FIS.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    settings = resolve_layout(frozen=True)

    assert settings.data_root == program / "data"
    # Batch working copies are machine-local, so the folder stays small.
    assert settings.work_root == tmp_path / "local" / "FBA-Invoice-Splitter"
    assert settings.batches_root == settings.work_root / "batches"


def test_the_environment_still_overrides_the_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIS_DATA_ROOT", str(tmp_path / "chosen"))

    assert resolve_layout(frozen=False).data_root == tmp_path / "chosen"


def test_an_unwritable_data_root_is_reported_not_relocated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falling back silently would leave the user copying a folder with no data.

    The obstruction is a file sitting where data/ needs to be, which blocks the
    write on every platform. chmod would not: on Windows it does nothing to a
    directory, so the read-only case simply would not arise.
    """

    program = tmp_path / "Program Files" / "FIS"
    program.mkdir(parents=True)
    (program / "data").write_text("not a directory", encoding="utf-8")
    monkeypatch.delenv("FIS_DATA_ROOT", raising=False)
    monkeypatch.setattr(sys, "executable", str(program / "FIS.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    with pytest.raises(StartupError, match="无法写入"):
        resolve_layout(frozen=True)


def test_a_writable_data_root_leaves_no_probe_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = tmp_path / "FIS"
    program.mkdir()
    monkeypatch.delenv("FIS_DATA_ROOT", raising=False)
    monkeypatch.setattr(sys, "executable", str(program / "FIS.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    settings = resolve_layout(frozen=True)

    assert list(settings.data_root.iterdir()) == []
