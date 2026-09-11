from dataclasses import dataclass
from datetime import UTC, datetime

from fis.domain.models import BatchState
from fis.storage.database import Database


@dataclass(frozen=True)
class BatchRecord:
    id: str
    state: BatchState


@dataclass(frozen=True)
class ChannelRecord:
    id: int
    name: str


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _current_timestamp() -> str:
    return datetime.now(UTC).isoformat()


class BatchRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(
        self,
        batch_id: str,
        filename: str,
        sha256: str,
        source_path: str,
        now: datetime,
    ) -> None:
        timestamp = _timestamp(now)
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO processing_batches(
                    id, source_filename, source_sha256, template_type, template_version,
                    state, source_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    filename,
                    sha256,
                    "fba-customs-standard",
                    "v1",
                    BatchState.PARSING.value,
                    source_path,
                    timestamp,
                    timestamp,
                ),
            )

    def set_state(
        self,
        batch_id: str,
        state: BatchState,
        now: datetime,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        timestamp = _timestamp(now)
        completed_at = timestamp if state is BatchState.COMPLETED else None
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE processing_batches
                SET state = ?, updated_at = ?, error_code = ?, error_detail = ?,
                    completed_at = CASE WHEN ? IS NOT NULL THEN ? ELSE completed_at END
                WHERE id = ?
                """,
                (
                    state.value,
                    timestamp,
                    error_code,
                    error_detail,
                    completed_at,
                    completed_at,
                    batch_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(batch_id)

    def get(self, batch_id: str) -> BatchRecord:
        connection = self._database.connect()
        try:
            row = connection.execute(
                "SELECT id, state FROM processing_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError(batch_id)
        return BatchRecord(id=row["id"], state=BatchState(row["state"]))

    def list_history(self) -> list[BatchRecord]:
        connection = self._database.connect()
        try:
            rows = connection.execute(
                """
                SELECT id, state
                FROM processing_batches
                WHERE state = ?
                ORDER BY completed_at DESC
                """,
                (BatchState.COMPLETED.value,),
            ).fetchall()
        finally:
            connection.close()
        return [BatchRecord(id=row["id"], state=BatchState(row["state"])) for row in rows]

    def list_interrupted(self) -> list[BatchRecord]:
        connection = self._database.connect()
        try:
            rows = connection.execute(
                """
                SELECT id, state
                FROM processing_batches
                WHERE state IN (?, ?)
                ORDER BY created_at ASC, rowid ASC
                """,
                (BatchState.PARSING.value, BatchState.GENERATING.value),
            ).fetchall()
        finally:
            connection.close()
        return [BatchRecord(id=row["id"], state=BatchState(row["state"])) for row in rows]


class MasterDataRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create_channel(self, name: str) -> int:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("channel name must not be blank")
        timestamp = _current_timestamp()
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO shipping_channels(name, created_at, updated_at)
                VALUES (?, ?, ?)
                """,
                (normalized_name, timestamp, timestamp),
            )
            channel_id = cursor.lastrowid
        if channel_id is None:
            raise RuntimeError("channel insert did not return an ID")
        return channel_id

    def create_fba_warehouse(self, code: str, address: str) -> None:
        normalized_code = code.strip().upper()
        normalized_address = address.strip()
        if not normalized_code:
            raise ValueError("warehouse code must not be blank")
        if not normalized_address:
            raise ValueError("warehouse address must not be blank")
        timestamp = _current_timestamp()
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO fba_warehouses(code, address, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (normalized_code, normalized_address, timestamp, timestamp),
            )

    def record_successful_channel(self, code: str, channel_id: int) -> None:
        normalized_code = code.strip().upper()
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE fba_warehouses
                SET last_successful_channel_id = ?, updated_at = ?
                WHERE code = ?
                """,
                (channel_id, _current_timestamp(), normalized_code),
            )
            if cursor.rowcount == 0:
                raise KeyError(normalized_code)

    def get_recent_channel(self, code: str) -> ChannelRecord | None:
        normalized_code = code.strip().upper()
        connection = self._database.connect()
        try:
            row = connection.execute(
                """
                SELECT channel.id, channel.name
                FROM fba_warehouses AS warehouse
                JOIN shipping_channels AS channel
                    ON channel.id = warehouse.last_successful_channel_id
                WHERE warehouse.code = ?
                """,
                (normalized_code,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return ChannelRecord(id=row["id"], name=row["name"])


class DefaultsRepository:
    """Persist last-used defaults as small key/value strings only."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def set(self, key: str, value: str) -> None:
        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError("default key must not be blank")
        if not isinstance(value, str):
            raise TypeError("default value must be a string")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO app_defaults(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (normalized_key, value, _current_timestamp()),
            )

    def get(self, key: str) -> str | None:
        connection = self._database.connect()
        try:
            row = connection.execute(
                "SELECT value FROM app_defaults WHERE key = ?",
                (key.strip(),),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else row["value"]

    def all(self) -> dict[str, str]:
        connection = self._database.connect()
        try:
            rows = connection.execute("SELECT key, value FROM app_defaults").fetchall()
        finally:
            connection.close()
        return {row["key"]: row["value"] for row in rows}


__all__ = [
    "BatchRecord",
    "BatchRepository",
    "ChannelRecord",
    "DefaultsRepository",
    "MasterDataRepository",
]
