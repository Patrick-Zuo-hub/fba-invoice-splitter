import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from fis.domain.models import BatchState
from fis.storage.database import Database
from fis.storage.repositories import (
    BatchRecord,
    BatchRepository,
    ChannelRecord,
    MasterDataRepository,
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "fis.db")
    database.initialize()
    return database


def create_batch(
    repository: BatchRepository,
    batch_id: str,
    now: datetime,
) -> None:
    repository.create(
        batch_id=batch_id,
        filename=f"{batch_id}.xlsx",
        sha256="a" * 64,
        source_path=f"batches/{batch_id}/source.xlsx",
        now=now,
    )


def test_newly_created_batch_is_not_in_history(database: Database) -> None:
    repository = BatchRepository(database)
    create_batch(repository, "batch-1", datetime(2026, 9, 7, 9, 0, tzinfo=UTC))

    assert repository.list_history() == []


def test_completed_batch_appears_in_history(database: Database) -> None:
    repository = BatchRepository(database)
    now = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
    create_batch(repository, "batch-1", now)

    repository.set_state("batch-1", BatchState.COMPLETED, now + timedelta(minutes=1))

    assert repository.list_history() == [BatchRecord("batch-1", BatchState.COMPLETED)]


def test_history_excludes_noncompleted_batches_and_sorts_newest_first(
    database: Database,
) -> None:
    repository = BatchRepository(database)
    now = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
    for batch_id in ("older", "newer", "parsing", "generating", "incomplete"):
        create_batch(repository, batch_id, now)

    repository.set_state("older", BatchState.COMPLETED, now + timedelta(minutes=1))
    repository.set_state("newer", BatchState.COMPLETED, now + timedelta(minutes=5))
    repository.set_state("generating", BatchState.GENERATING, now + timedelta(minutes=2))
    repository.set_state("incomplete", BatchState.INCOMPLETE, now + timedelta(minutes=3))

    assert repository.list_history() == [
        BatchRecord("newer", BatchState.COMPLETED),
        BatchRecord("older", BatchState.COMPLETED),
    ]


def test_history_sorts_mixed_offsets_by_real_instant(database: Database) -> None:
    repository = BatchRepository(database)
    created_at = datetime(2026, 9, 7, 1, 0, tzinfo=UTC)
    older_instant = datetime(
        2026,
        9,
        7,
        10,
        0,
        tzinfo=timezone(timedelta(hours=8)),
    )
    newer_instant = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
    create_batch(repository, "older", created_at)
    create_batch(repository, "newer", created_at)

    repository.set_state("older", BatchState.COMPLETED, older_instant)
    repository.set_state("newer", BatchState.COMPLETED, newer_instant)

    assert repository.list_history() == [
        BatchRecord("newer", BatchState.COMPLETED),
        BatchRecord("older", BatchState.COMPLETED),
    ]


def test_create_persists_standard_template_state_and_source_metadata(
    database: Database,
) -> None:
    repository = BatchRepository(database)
    now = datetime(2026, 9, 7, 9, 15, 20, tzinfo=timezone(timedelta(hours=8)))

    repository.create(
        batch_id="batch-1",
        filename="source.xlsx",
        sha256="b" * 64,
        source_path="batches/batch-1/source.xlsx",
        now=now,
    )

    with closing(database.connect()) as connection:
        row = connection.execute(
            """
            SELECT source_filename, source_sha256, template_type, template_version,
                   state, source_path, created_at, updated_at, completed_at
            FROM processing_batches
            WHERE id = ?
            """,
            ("batch-1",),
        ).fetchone()
    assert row is not None
    assert tuple(row) == (
        "source.xlsx",
        "b" * 64,
        "fba-customs-standard",
        "v1",
        "parsing",
        "batches/batch-1/source.xlsx",
        now.astimezone(UTC).isoformat(),
        now.astimezone(UTC).isoformat(),
        None,
    )


def test_batch_repository_rejects_naive_datetimes(database: Database) -> None:
    repository = BatchRepository(database)
    naive = datetime(2026, 9, 7, 9, 0)  # noqa: DTZ001 - deliberately invalid input

    with pytest.raises(ValueError, match="timezone-aware"):
        create_batch(repository, "batch-1", naive)

    create_batch(repository, "batch-1", naive.replace(tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone-aware"):
        repository.set_state("batch-1", BatchState.COMPLETED, naive)


def test_set_state_updates_errors_timestamps_and_completed_timestamp(
    database: Database,
) -> None:
    repository = BatchRepository(database)
    created_at = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
    china_time = timezone(timedelta(hours=8))
    failed_at = datetime(2026, 9, 7, 17, 1, tzinfo=china_time)
    completed_at = datetime(2026, 9, 7, 17, 2, tzinfo=china_time)
    create_batch(repository, "batch-1", created_at)

    repository.set_state(
        "batch-1",
        BatchState.INCOMPLETE,
        failed_at,
        error_code="INVALID_FILE",
        error_detail="Missing shipment ID",
    )
    with closing(database.connect()) as connection:
        failed = connection.execute(
            """
            SELECT state, updated_at, completed_at, error_code, error_detail
            FROM processing_batches WHERE id = ?
            """,
            ("batch-1",),
        ).fetchone()
    assert failed is not None
    assert tuple(failed) == (
        "incomplete",
        failed_at.astimezone(UTC).isoformat(),
        None,
        "INVALID_FILE",
        "Missing shipment ID",
    )

    repository.set_state("batch-1", BatchState.COMPLETED, completed_at)

    with closing(database.connect()) as connection:
        completed = connection.execute(
            """
            SELECT state, updated_at, completed_at, error_code, error_detail
            FROM processing_batches WHERE id = ?
            """,
            ("batch-1",),
        ).fetchone()
    assert completed is not None
    assert tuple(completed) == (
        "completed",
        completed_at.astimezone(UTC).isoformat(),
        completed_at.astimezone(UTC).isoformat(),
        None,
        None,
    )


def test_missing_batch_state_update_and_get_raise_key_error(database: Database) -> None:
    repository = BatchRepository(database)

    with pytest.raises(KeyError, match="missing"):
        repository.set_state(
            "missing",
            BatchState.GENERATING,
            datetime(2026, 9, 7, 9, 0, tzinfo=UTC),
        )
    with pytest.raises(KeyError, match="missing"):
        repository.get("missing")


def test_get_returns_batch_record(database: Database) -> None:
    repository = BatchRepository(database)
    create_batch(repository, "batch-1", datetime(2026, 9, 7, 9, 0, tzinfo=UTC))

    assert repository.get("batch-1") == BatchRecord("batch-1", BatchState.PARSING)


def test_list_interrupted_returns_only_parsing_and_generating(database: Database) -> None:
    repository = BatchRepository(database)
    now = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
    for batch_id in ("parsing", "generating", "awaiting", "completed", "incomplete"):
        create_batch(repository, batch_id, now)
    repository.set_state("generating", BatchState.GENERATING, now)
    repository.set_state("awaiting", BatchState.AWAITING_CONFIRMATION, now)
    repository.set_state("completed", BatchState.COMPLETED, now)
    repository.set_state("incomplete", BatchState.INCOMPLETE, now)

    assert repository.list_interrupted() == [
        BatchRecord("parsing", BatchState.PARSING),
        BatchRecord("generating", BatchState.GENERATING),
    ]


def test_create_channel_returns_id_and_persists_trimmed_name(database: Database) -> None:
    repository = MasterDataRepository(database)

    channel_id = repository.create_channel("  Ocean Express  ")

    assert isinstance(channel_id, int)
    with closing(database.connect()) as connection:
        row = connection.execute(
            "SELECT name, created_at, updated_at FROM shipping_channels WHERE id = ?",
            (channel_id,),
        ).fetchone()
    assert row is not None
    assert row["name"] == "Ocean Express"
    assert datetime.fromisoformat(row["created_at"]).tzinfo is not None
    assert datetime.fromisoformat(row["updated_at"]).tzinfo is not None


def test_warehouse_code_is_normalized_for_successful_channel_lookup(
    database: Database,
) -> None:
    repository = MasterDataRepository(database)
    channel_id = repository.create_channel("Ocean Express")
    repository.create_fba_warehouse("  lax9 ", "  123 Harbor Road  ")

    repository.record_successful_channel(" lax9 ", channel_id)

    assert repository.get_recent_channel("LaX9") == ChannelRecord(channel_id, "Ocean Express")
    with closing(database.connect()) as connection:
        warehouse = connection.execute(
            "SELECT code, address FROM fba_warehouses WHERE code = ?", ("AMAZON-LAX9",)
        ).fetchone()
    assert warehouse is not None
    assert tuple(warehouse) == ("AMAZON-LAX9", "123 Harbor Road")


def test_no_recent_channel_before_explicit_success_record(database: Database) -> None:
    repository = MasterDataRepository(database)
    repository.create_channel("Ocean Express")
    repository.create_fba_warehouse("LAX9", "123 Harbor Road")

    assert repository.get_recent_channel(" lax9 ") is None


def test_duplicate_warehouse_does_not_overwrite_address(database: Database) -> None:
    repository = MasterDataRepository(database)
    repository.create_fba_warehouse("lax9", "Original address")

    with pytest.raises(sqlite3.IntegrityError):
        repository.create_fba_warehouse(" LAX9 ", "Replacement address")

    with closing(database.connect()) as connection:
        address = connection.execute(
            "SELECT address FROM fba_warehouses WHERE code = ?", ("AMAZON-LAX9",)
        ).fetchone()
    assert address is not None
    assert address["address"] == "Original address"


def test_delivery_warehouse_address_can_be_blank_and_cleared(database: Database) -> None:
    repository = MasterDataRepository(database)

    warehouse_id = repository.add_delivery_warehouse("宁波仓", "")
    repository.update_delivery_warehouse(warehouse_id, address="  ")

    warehouse = repository.list_delivery_warehouses()[0]
    assert warehouse.name == "宁波仓"
    assert warehouse.address == ""


@pytest.mark.parametrize("name", ["", " ", "\t\n"])
def test_blank_channel_name_is_rejected(database: Database, name: str) -> None:
    with pytest.raises(ValueError, match="channel name"):
        MasterDataRepository(database).create_channel(name)


@pytest.mark.parametrize(
    ("code", "address", "message"),
    [("", "Somewhere", "warehouse code"), ("LAX9", "  ", "warehouse address")],
)
def test_blank_warehouse_fields_are_rejected(
    database: Database,
    code: str,
    address: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        MasterDataRepository(database).create_fba_warehouse(code, address)


def test_record_successful_channel_raises_for_missing_warehouse(database: Database) -> None:
    repository = MasterDataRepository(database)
    channel_id = repository.create_channel("Ocean Express")

    with pytest.raises(KeyError, match="MISSING"):
        repository.record_successful_channel(" missing ", channel_id)


def test_repository_read_connections_are_closed(database: Database) -> None:
    batch_repository = BatchRepository(database)
    master_data_repository = MasterDataRepository(database)
    now = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
    create_batch(batch_repository, "batch-1", now)
    master_data_repository.create_fba_warehouse("LAX9", "123 Harbor Road")
    captured: list[sqlite3.Connection] = []
    original_connect = database.connect

    def capture_connection() -> sqlite3.Connection:
        connection = original_connect()
        captured.append(connection)
        return connection

    database.connect = capture_connection  # type: ignore[method-assign]

    batch_repository.get("batch-1")
    batch_repository.list_history()
    batch_repository.list_interrupted()
    master_data_repository.get_recent_channel("LAX9")

    assert len(captured) == 4
    for connection in captured:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")
