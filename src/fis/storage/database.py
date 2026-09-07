import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fis.storage.schema import SCHEMA_V1


def _iter_sql_statements(script: str) -> Iterator[str]:
    lines: list[str] = []
    for line in script.splitlines():
        lines.append(line)
        statement = "\n".join(lines).strip()
        if statement and sqlite3.complete_statement(statement):
            yield statement
            lines.clear()

    remainder = "\n".join(lines).strip()
    if remainder:
        raise RuntimeError("SCHEMA_V1 contains an incomplete SQL statement")


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
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if not exists:
                for statement in _iter_sql_statements(SCHEMA_V1):
                    connection.execute(statement)
            else:
                version_rows = connection.execute("SELECT version FROM schema_version").fetchall()
                if len(version_rows) != 1 or version_rows[0]["version"] != 1:
                    raise RuntimeError(
                        "schema_version must contain exactly one row with supported version 1"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
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
