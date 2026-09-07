import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from fis.storage import database as database_module
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


def test_initialize_rolls_back_partial_schema_and_allows_clean_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "fis.db"
    database = Database(database_path)
    valid_schema = database_module.SCHEMA_V1
    failing_schema = valid_schema.replace(
        "CREATE TABLE delivery_warehouses (",
        "CREATE TABL delivery_warehouses (",
        1,
    )
    monkeypatch.setattr(database_module, "SCHEMA_V1", failing_schema)

    with pytest.raises(sqlite3.OperationalError):
        database.initialize()

    with closing(sqlite3.connect(database_path)) as connection:
        partial_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert EXPECTED_TABLES.isdisjoint(partial_tables)

    monkeypatch.setattr(database_module, "SCHEMA_V1", valid_schema)
    database.initialize()

    with closing(sqlite3.connect(database_path)) as connection:
        version_rows = connection.execute("SELECT version FROM schema_version").fetchall()
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert version_rows == [(1,)]
    assert EXPECTED_TABLES <= table_names


def test_concurrent_initialize_calls_both_succeed(tmp_path: Path) -> None:
    database_path = tmp_path / "fis.db"
    connections_ready = threading.Barrier(2)
    existence_checks = threading.Barrier(2)

    class CoordinatedConnection(sqlite3.Connection):
        def execute(
            self,
            sql: str,
            parameters: tuple[object, ...] = (),
        ) -> sqlite3.Cursor:
            cursor = super().execute(sql, parameters)
            if "FROM sqlite_master" in sql and not self.in_transaction:
                existence_checks.wait(timeout=5)
            return cursor

    class CoordinatedDatabase(Database):
        def connect(self) -> sqlite3.Connection:
            connection = sqlite3.connect(database_path, factory=CoordinatedConnection)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connections_ready.wait(timeout=5)
            return connection

    databases = (CoordinatedDatabase(database_path), CoordinatedDatabase(database_path))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(database.initialize) for database in databases]
        for future in futures:
            future.result(timeout=10)

    with closing(sqlite3.connect(database_path)) as connection:
        version_rows = connection.execute("SELECT version FROM schema_version").fetchall()
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert version_rows == [(1,)]
    assert EXPECTED_TABLES <= table_names


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


def test_deleting_batch_cascades_through_shipments_and_items(tmp_path: Path) -> None:
    database = Database(tmp_path / "fis.db")
    database.initialize()

    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO processing_batches(
                id, source_filename, source_sha256, template_type, template_version,
                state, source_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "batch-1",
                "source.xlsx",
                "a" * 64,
                "shipment-export",
                "1",
                "uploaded",
                "batches/batch-1/source.xlsx",
                "2026-09-07T10:00:00Z",
                "2026-09-07T10:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO shipments(batch_id, shipment_id, snapshot_json)
            VALUES (?, ?, ?)
            """,
            ("batch-1", "shipment-1", "{}"),
        )
        connection.execute(
            """
            INSERT INTO shipment_items(
                batch_id, shipment_id, source_row, fields_json, drawing_ids_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("batch-1", "shipment-1", 2, "{}", "[]"),
        )

    with database.transaction() as connection:
        connection.execute("DELETE FROM processing_batches WHERE id = ?", ("batch-1",))

    with closing(database.connect()) as connection:
        shipment_count = connection.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]
        item_count = connection.execute("SELECT COUNT(*) FROM shipment_items").fetchone()[0]
    assert shipment_count == 0
    assert item_count == 0


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
