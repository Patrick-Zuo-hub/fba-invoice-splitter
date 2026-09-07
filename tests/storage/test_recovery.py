from datetime import UTC, datetime
from pathlib import Path

import pytest

from fis.domain.models import BatchState
from fis.storage.batch_files import BatchFiles
from fis.storage.database import Database
from fis.storage.recovery import recover_interrupted
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
    source = files.work_path("batch-1")
    source.mkdir(parents=True)
    (source / "source.xlsx").write_bytes(b"source")
    output = files.publish("batch-1")
    (output / "result.xlsx").write_bytes(b"output")
    files.create("batch-1")

    recover_interrupted(repository, files, FIXED_NOW)

    assert (output / "result.xlsx").read_bytes() == b"output"
    assert (output / "source.xlsx").read_bytes() == b"source"


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
