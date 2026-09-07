import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fis.storage.schema import SCHEMA_V1


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if not exists:
                connection.executescript(SCHEMA_V1)
                connection.commit()
                return

            version_rows = connection.execute("SELECT version FROM schema_version").fetchall()
            if len(version_rows) != 1 or version_rows[0]["version"] != 1:
                raise RuntimeError(
                    "schema_version must contain exactly one row with supported version 1"
                )
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
