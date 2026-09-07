import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from fis.storage.database import Database

EXPECTED_TABLES = {
    "delivery_warehouses",
    "fba_warehouses",
    "processing_batches",
    "schema_version",
    "shipment_items",
    "shipments",
    "shipping_channels",
}


def test_initialize_creates_parent_directory_and_schema_version(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "fis.db"

    Database(database_path).initialize()

    assert database_path.parent.is_dir()
    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [(1,)]


def test_initialize_creates_required_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "fis.db"

    Database(database_path).initialize()

    with closing(sqlite3.connect(database_path)) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert EXPECTED_TABLES <= table_names


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "fis.db"
    database = Database(database_path)

    database.initialize()
    database.initialize()

    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchall() == [(1,)]


@pytest.mark.parametrize(
    "versions",
    [
        [],
        [1, 1],
        [2],
    ],
    ids=["empty", "multiple-rows", "unsupported-version"],
)
def test_initialize_rejects_invalid_schema_version_state(
    tmp_path: Path,
    versions: list[int],
) -> None:
    database_path = tmp_path / "fis.db"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
        connection.executemany(
            "INSERT INTO schema_version(version) VALUES (?)",
            [(version,) for version in versions],
        )
        connection.commit()

    with pytest.raises(RuntimeError, match=r"schema_version.*exactly one row.*version 1"):
        Database(database_path).initialize()


def test_transaction_commits_on_success(tmp_path: Path) -> None:
    database = Database(tmp_path / "fis.db")
    database.initialize()

    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO shipping_channels(name, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            ("Ocean", "2026-09-07T10:00:00Z", "2026-09-07T10:00:00Z"),
        )

    with closing(database.connect()) as connection:
        saved = connection.execute(
            "SELECT name FROM shipping_channels WHERE name = ?", ("Ocean",)
        ).fetchone()
    assert saved is not None
    assert saved["name"] == "Ocean"


def test_transaction_rolls_back_on_exception(tmp_path: Path) -> None:
    database = Database(tmp_path / "fis.db")
    database.initialize()

    with (
        pytest.raises(ValueError, match="abort transaction"),
        database.transaction() as connection,
    ):
        connection.execute(
            """
            INSERT INTO shipping_channels(name, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            ("Ocean", "2026-09-07T10:00:00Z", "2026-09-07T10:00:00Z"),
        )
        raise ValueError("abort transaction")

    with closing(database.connect()) as connection:
        saved = connection.execute(
            "SELECT name FROM shipping_channels WHERE name = ?", ("Ocean",)
        ).fetchone()
    assert saved is None


def test_connect_enables_foreign_keys_and_rejects_orphan_shipment(tmp_path: Path) -> None:
    database = Database(tmp_path / "fis.db")
    database.initialize()

    with closing(database.connect()) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO shipments(batch_id, shipment_id, snapshot_json)
                VALUES (?, ?, ?)
                """,
                ("missing-batch", "SHIPMENT-1", "{}"),
            )


def test_initialize_closes_its_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "fis.db")
    connections: list[sqlite3.Connection] = []
    original_connect = database.connect

    def capture_connection() -> sqlite3.Connection:
        connection = original_connect()
        connections.append(connection)
        return connection

    monkeypatch.setattr(database, "connect", capture_connection)

    database.initialize()

    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[0].execute("SELECT 1")


def test_transaction_closes_its_connection(tmp_path: Path) -> None:
    database = Database(tmp_path / "fis.db")
    database.initialize()
    transaction_connection: sqlite3.Connection | None = None

    with database.transaction() as connection:
        transaction_connection = connection

    assert transaction_connection is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        transaction_connection.execute("SELECT 1")
