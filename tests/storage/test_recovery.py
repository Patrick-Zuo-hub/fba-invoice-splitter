import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fis.domain.models import BatchState
from fis.storage.batch_files import BatchFiles
from fis.storage.database import Database
from fis.storage.recovery import recover_interrupted, run_startup_maintenance
from fis.storage.repositories import BatchRepository

FIXED_NOW = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "fis.db")
    database.initialize()
    return database


def _create_batch(repository: BatchRepository, batch_id: str) -> None:
    repository.create(
        batch_id,
        "source.xlsx",
        "a" * 64,
        f"batches/{batch_id}/source.xlsx",
        FIXED_NOW,
    )


def test_recovery_marks_parsing_batch_incomplete_and_clears_work(
    database: Database,
    tmp_path: Path,
) -> None:
    repository = BatchRepository(database)
    files = BatchFiles(tmp_path / "data")
    _create_batch(repository, "batch-parsing")
    work = files.create("batch-parsing")
    (work / "partial.txt").write_text("partial")

    recover_interrupted(repository, files, FIXED_NOW)

    assert repository.get("batch-parsing").state is BatchState.INCOMPLETE
    assert not files.work_path("batch-parsing").exists()


def test_recovery_marks_generating_batch_incomplete_and_records_error(
    database: Database,
    tmp_path: Path,
) -> None:
    repository = BatchRepository(database)
    files = BatchFiles(tmp_path / "data")
    _create_batch(repository, "batch-generating")
    repository.set_state("batch-generating", BatchState.GENERATING, FIXED_NOW)
    files.create("batch-generating")

    recover_interrupted(repository, files, FIXED_NOW)

    record = repository.get("batch-generating")
    assert record.state is BatchState.INCOMPLETE
    with database.connect() as connection:
        row = connection.execute(
            "SELECT error_code, error_detail FROM processing_batches WHERE id = ?",
            ("batch-generating",),
        ).fetchone()
    assert row is not None
    assert row["error_code"] == "INTERRUPTED"
    assert row["error_detail"]


def test_recovery_preserves_source_and_output(
    database: Database,
    tmp_path: Path,
) -> None:
    repository = BatchRepository(database)
    files = BatchFiles(tmp_path / "data")
    _create_batch(repository, "batch-1")
    batch_dir = files.work_path("batch-1").parent
    source = batch_dir / "source"
    source.mkdir(parents=True)
    (source / "source.xlsx").write_bytes(b"source")
    output = batch_dir / "output"
    output.mkdir()
    (output / "result.xlsx").write_bytes(b"output")
    work = files.create("batch-1")
    (work / "partial.xlsx").write_bytes(b"partial")

    recover_interrupted(repository, files, FIXED_NOW)

    assert (source / "source.xlsx").read_bytes() == b"source"
    assert (output / "result.xlsx").read_bytes() == b"output"
    assert not work.exists()


def test_recovery_leaves_noninterrupted_states_unchanged(
    database: Database,
    tmp_path: Path,
) -> None:
    repository = BatchRepository(database)
    files = BatchFiles(tmp_path / "data")
    for batch_id in ("batch-awaiting", "batch-completed", "batch-incomplete"):
        _create_batch(repository, batch_id)
    repository.set_state("batch-awaiting", BatchState.AWAITING_CONFIRMATION, FIXED_NOW)
    repository.set_state("batch-completed", BatchState.COMPLETED, FIXED_NOW)
    repository.set_state("batch-incomplete", BatchState.INCOMPLETE, FIXED_NOW)

    recover_interrupted(repository, files, FIXED_NOW)

    assert repository.get("batch-awaiting").state is BatchState.AWAITING_CONFIRMATION
    assert repository.get("batch-completed").state is BatchState.COMPLETED
    assert repository.get("batch-incomplete").state is BatchState.INCOMPLETE


def test_recovery_rejects_naive_now_before_changing_batches(
    database: Database,
    tmp_path: Path,
) -> None:
    repository = BatchRepository(database)
    files = BatchFiles(tmp_path / "data")
    _create_batch(repository, "batch-1")
    files.create("batch-1")
    naive = datetime(2026, 9, 7, 9, 0)  # noqa: DTZ001 - deliberately invalid input

    with pytest.raises(ValueError, match="timezone-aware"):
        recover_interrupted(repository, files, naive)

    assert repository.get("batch-1").state is BatchState.PARSING
    assert files.work_path("batch-1").is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory handles are only available on Windows")
def test_windows_recovery_clears_work_and_marks_batch_incomplete(
    database: Database,
    tmp_path: Path,
) -> None:
    repository = BatchRepository(database)
    files = BatchFiles(tmp_path / "data")
    _create_batch(repository, "batch-1")
    work = files.create("batch-1")
    (work / "partial.xlsx").write_bytes(b"partial")

    recover_interrupted(repository, files, FIXED_NOW)

    assert repository.get("batch-1").state is BatchState.INCOMPLETE
    assert not work.exists()


def test_startup_maintenance_discards_every_batch_working_directory(database: Database, tmp_path: Path) -> None:
    """After a restart no workspace references them: WorkspaceStore is per-process.

    Nothing rebuilds a workspace from the database, so a batch directory that
    survives a restart can never be read again — it is pure accumulation.
    """

    repository = BatchRepository(database)
    files = BatchFiles(tmp_path / "data")
    for batch_id in ("batch-alpha", "batch-beta", "batch-gamma"):
        _create_batch(repository, batch_id)
        (files.create(batch_id) / "source.xlsx").write_bytes(b"upload")
    repository.set_state("batch-alpha", BatchState.COMPLETED, FIXED_NOW)

    run_startup_maintenance(repository, files, FIXED_NOW)

    assert files.list_batch_ids() == ()
    assert not files.batches_root.exists() or list(files.batches_root.iterdir()) == []


def test_startup_maintenance_keeps_history_readable(database: Database, tmp_path: Path) -> None:
    """Removing the working files must not remove the record of the batch."""

    repository = BatchRepository(database)
    files = BatchFiles(tmp_path / "data")
    _create_batch(repository, "batch-alpha")
    files.create("batch-alpha")
    repository.set_state("batch-alpha", BatchState.COMPLETED, FIXED_NOW)

    run_startup_maintenance(repository, files, FIXED_NOW)

    assert [record.id for record in repository.list_history()] == ["batch-alpha"]


def test_startup_maintenance_still_marks_interrupted_batches(database: Database, tmp_path: Path) -> None:
    repository = BatchRepository(database)
    files = BatchFiles(tmp_path / "data")
    _create_batch(repository, "batch-alpha")
    files.create("batch-alpha")
    repository.set_state("batch-alpha", BatchState.GENERATING, FIXED_NOW)

    run_startup_maintenance(repository, files, FIXED_NOW)

    assert repository.get("batch-alpha").state is BatchState.INCOMPLETE


def test_startup_maintenance_removes_a_directory_with_no_record(database: Database, tmp_path: Path) -> None:
    """A crash between mkdir and insert leaves an orphan nobody would notice."""

    repository = BatchRepository(database)
    files = BatchFiles(tmp_path / "data")
    files.create("batch-orphan")

    run_startup_maintenance(repository, files, FIXED_NOW)

    assert files.list_batch_ids() == ()


def test_startup_maintenance_survives_a_directory_it_cannot_remove(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup is housekeeping: it must never stop the application starting."""

    repository = BatchRepository(database)
    files = BatchFiles(tmp_path / "data")
    _create_batch(repository, "batch-alpha")
    files.create("batch-alpha")

    def refuse(batch_id: str) -> None:
        raise OSError("busy")

    monkeypatch.setattr(files, "delete_batch", refuse)

    run_startup_maintenance(repository, files, FIXED_NOW)
