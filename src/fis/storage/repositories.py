from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fis.domain.models import BatchState
from fis.domain.rules import canonical_amazon_warehouse_code
from fis.storage.database import Database

if TYPE_CHECKING:
    from fis.xlsx.masters import MasterData


@dataclass(frozen=True)
class BatchRecord:
    id: str
    state: BatchState


@dataclass(frozen=True)
class ChannelRecord:
    id: int
    name: str


@dataclass(frozen=True)
class ShipmentHistory:
    shipment_id: str
    output_path: str


@dataclass(frozen=True)
class HistoryEntry:
    id: str
    source_filename: str
    state: BatchState
    completed_at: str | None
    error_code: str | None
    shipments: tuple[ShipmentHistory, ...]
    output_directory: str | None
    output_path: str | None = None


@dataclass(frozen=True)
class MasterChannel:
    id: int
    name: str
    active: bool
    sort_order: int


@dataclass(frozen=True)
class MasterDeliveryWarehouse:
    id: int
    name: str
    address: str
    active: bool
    sort_order: int


@dataclass(frozen=True)
class MasterFbaWarehouse:
    code: str
    name: str
    address: str
    active: bool


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


    def record_completed(
        self,
        *,
        batch_id: str,
        filename: str,
        sha256: str,
        source_path: str,
        output_path: str,
        shipment_count: int,
        item_count: int,
        shipments: list[tuple[str, str]],
        now: datetime,
    ) -> None:
        timestamp = _timestamp(now)
        with self._database.transaction() as connection:
            connection.execute("DELETE FROM processing_batches WHERE id = ?", (batch_id,))
            connection.execute(
                """
                INSERT INTO processing_batches(
                    id, source_filename, source_sha256, template_type, template_version,
                    state, shipment_count, item_count, source_path, output_path,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    filename,
                    sha256,
                    "fba-customs-standard",
                    "v1",
                    BatchState.COMPLETED.value,
                    shipment_count,
                    item_count,
                    source_path,
                    output_path,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            for shipment_id, snapshot_json in shipments:
                connection.execute(
                    """
                    INSERT INTO shipments(batch_id, shipment_id, snapshot_json, sailing_manual)
                    VALUES (?, ?, ?, 0)
                    """,
                    (batch_id, shipment_id, snapshot_json),
                )

    def record_failed(
        self,
        *,
        batch_id: str,
        filename: str,
        sha256: str,
        source_path: str,
        shipment_count: int,
        error_code: str,
        error_detail: str | None,
        now: datetime,
    ) -> None:
        timestamp = _timestamp(now)
        with self._database.transaction() as connection:
            connection.execute("DELETE FROM processing_batches WHERE id = ?", (batch_id,))
            connection.execute(
                """
                INSERT INTO processing_batches(
                    id, source_filename, source_sha256, template_type, template_version,
                    state, shipment_count, item_count, source_path,
                    error_code, error_detail, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    filename,
                    sha256,
                    "fba-customs-standard",
                    "v1",
                    BatchState.INCOMPLETE.value,
                    shipment_count,
                    0,
                    source_path,
                    error_code,
                    error_detail,
                    timestamp,
                    timestamp,
                ),
            )

    def list_history_entries(self) -> list[HistoryEntry]:
        connection = self._database.connect()
        try:
            batch_rows = connection.execute(
                """
                SELECT id, source_filename, state, completed_at, error_code, output_path
                FROM processing_batches
                WHERE state IN (?, ?)
                ORDER BY COALESCE(completed_at, updated_at) DESC, rowid DESC
                """,
                (BatchState.COMPLETED.value, BatchState.INCOMPLETE.value),
            ).fetchall()
            entries: list[HistoryEntry] = []
            for batch in batch_rows:
                shipment_rows = connection.execute(
                    """
                    SELECT shipment_id, output_path
                    FROM shipments
                    WHERE batch_id = ?
                    ORDER BY shipment_id
                    """,
                    (batch["id"],),
                ).fetchall()
                shipments = tuple(
                    ShipmentHistory(row["shipment_id"], row["output_path"] or "")
                    for row in shipment_rows
                )
                batch_output = batch["output_path"] or None
                directory: str | None = None
                if batch_output:
                    directory = os.path.dirname(batch_output)
                else:
                    for row in shipment_rows:
                        if row["output_path"]:
                            directory = os.path.dirname(row["output_path"])
                            break
                entries.append(
                    HistoryEntry(
                        id=batch["id"],
                        source_filename=batch["source_filename"],
                        state=BatchState(batch["state"]),
                        completed_at=batch["completed_at"],
                        error_code=batch["error_code"],
                        shipments=shipments,
                        output_directory=directory,
                        output_path=batch_output,
                    )
                )
            return entries
        finally:
            connection.close()

    def output_directory(self, batch_id: str) -> str | None:
        connection = self._database.connect()
        try:
            row = connection.execute(
                """
                SELECT output_path FROM processing_batches
                WHERE id = ? AND output_path IS NOT NULL
                """,
                (batch_id,),
            ).fetchone()
            if row is None or not row["output_path"]:
                row = connection.execute(
                    """
                    SELECT output_path FROM shipments
                    WHERE batch_id = ? AND output_path IS NOT NULL
                    LIMIT 1
                    """,
                    (batch_id,),
                ).fetchone()
        finally:
            connection.close()
        if row is None or not row["output_path"]:
            return None
        return os.path.dirname(str(row["output_path"]))



@dataclass(frozen=True, slots=True)
class MasterImportCounts:
    added: int = 0
    updated: int = 0


@dataclass(frozen=True, slots=True)
class MasterImportSummary:
    """What one master-data import changed. Unchanged rows count as neither."""

    fba_warehouses: MasterImportCounts
    channels: MasterImportCounts
    delivery_warehouses: MasterImportCounts


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
        normalized_code = canonical_amazon_warehouse_code(code)
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

    def import_master_data(self, data: MasterData) -> MasterImportSummary:
        """Upsert a whole master-data workbook in one transaction.

        The uploaded file is authoritative for anything it names, so a changed
        address overwrites the stored one — deliberately unlike
        ``create_fba_warehouse``, which refuses to. Anything the file omits is
        left untouched: history references channels and warehouses by name, so
        removing them would orphan existing records. Status is never touched
        either, because deactivating an entry is a deliberate local action.
        """

        timestamp = _current_timestamp()
        warehouses = MasterImportCounts()
        channels = MasterImportCounts()
        delivery = MasterImportCounts()

        with self._database.transaction() as connection:
            added = updated = 0
            for code, address in data.fba_warehouses:
                normalized_code = canonical_amazon_warehouse_code(code)
                normalized_address = address.strip()
                if not normalized_code or not normalized_address:
                    raise ValueError("FBA 仓库需要仓代码与地址")
                row = connection.execute(
                    "SELECT address FROM fba_warehouses WHERE code = ?", (normalized_code,)
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO fba_warehouses(code, address, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (normalized_code, normalized_address, timestamp, timestamp),
                    )
                    added += 1
                elif row["address"] != normalized_address:
                    connection.execute(
                        "UPDATE fba_warehouses SET address = ?, updated_at = ? WHERE code = ?",
                        (normalized_address, timestamp, normalized_code),
                    )
                    updated += 1
            warehouses = MasterImportCounts(added=added, updated=updated)

            added = 0
            for name in data.channels:
                normalized = name.strip()
                if not normalized:
                    raise ValueError("运输渠道名称不能为空")
                existing = connection.execute(
                    "SELECT id FROM shipping_channels WHERE name = ?", (normalized,)
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO shipping_channels(name, created_at, updated_at)
                        VALUES (?, ?, ?)
                        """,
                        (normalized, timestamp, timestamp),
                    )
                    added += 1
            channels = MasterImportCounts(added=added)

            added = 0
            for name in data.delivery_warehouses:
                normalized = name.strip()
                if not normalized:
                    raise ValueError("送货仓库名称不能为空")
                existing = connection.execute(
                    "SELECT id FROM delivery_warehouses WHERE name = ?", (normalized,)
                ).fetchone()
                if existing is None:
                    # The sheet carries names only; never read that as a blank
                    # address for a warehouse that already has one.
                    connection.execute(
                        """
                        INSERT INTO delivery_warehouses(name, address, created_at, updated_at)
                        VALUES (?, '', ?, ?)
                        """,
                        (normalized, timestamp, timestamp),
                    )
                    added += 1
            delivery = MasterImportCounts(added=added)

        return MasterImportSummary(
            fba_warehouses=warehouses, channels=channels, delivery_warehouses=delivery
        )

    def record_successful_channel(self, code: str, channel_id: int) -> None:
        normalized_code = canonical_amazon_warehouse_code(code)
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
        normalized_code = canonical_amazon_warehouse_code(code)
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


    # -- listing helpers --------------------------------------------------

    def _reject_duplicate(
        self,
        table: str,
        column: str,
        value: str,
        *,
        exclude_column: str | None = None,
        exclude_value: object | None = None,
    ) -> None:
        query = f"SELECT 1 FROM {table} WHERE LOWER({column}) = LOWER(?)"
        params: list[object] = [value]
        if exclude_column is not None:
            query += f" AND {exclude_column} <> ?"
            params.append(exclude_value)
        connection = self._database.connect()
        try:
            exists = connection.execute(query, params).fetchone()
        finally:
            connection.close()
        if exists is not None:
            raise ValueError(f"名称已存在：{value}")

    # -- channels ---------------------------------------------------------

    def list_channels(self, *, active_only: bool = False) -> list[MasterChannel]:
        query = "SELECT id, name, status, sort_order FROM shipping_channels"
        if active_only:
            query += " WHERE status = 'active'"
        query += " ORDER BY sort_order, name"
        connection = self._database.connect()
        try:
            rows = connection.execute(query).fetchall()
        finally:
            connection.close()
        return [
            MasterChannel(row["id"], row["name"], row["status"] == "active", row["sort_order"])
            for row in rows
        ]

    def add_channel(self, name: str) -> int:
        normalized = name.strip()
        if not normalized:
            raise ValueError("渠道名称不能为空")
        self._reject_duplicate("shipping_channels", "name", normalized)
        timestamp = _current_timestamp()
        with self._database.transaction() as connection:
            order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM shipping_channels"
            ).fetchone()[0]
            cursor = connection.execute(
                """
                INSERT INTO shipping_channels(name, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (normalized, order, timestamp, timestamp),
            )
            channel_id = cursor.lastrowid
        if channel_id is None:
            raise RuntimeError("channel insert did not return an ID")
        return channel_id

    def rename_channel(self, channel_id: int, name: str) -> None:
        normalized = name.strip()
        if not normalized:
            raise ValueError("渠道名称不能为空")
        self._reject_duplicate(
            "shipping_channels", "name", normalized, exclude_column="id", exclude_value=channel_id
        )
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE shipping_channels SET name = ?, updated_at = ? WHERE id = ?",
                (normalized, _current_timestamp(), channel_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(channel_id)

    def set_channel_active(self, channel_id: int, active: bool) -> None:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE shipping_channels SET status = ?, updated_at = ? WHERE id = ?",
                ("active" if active else "inactive", _current_timestamp(), channel_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(channel_id)

    # -- delivery warehouses ---------------------------------------------

    def list_delivery_warehouses(
        self, *, active_only: bool = False
    ) -> list[MasterDeliveryWarehouse]:
        query = "SELECT id, name, address, status, sort_order FROM delivery_warehouses"
        if active_only:
            query += " WHERE status = 'active'"
        query += " ORDER BY sort_order, name"
        connection = self._database.connect()
        try:
            rows = connection.execute(query).fetchall()
        finally:
            connection.close()
        return [
            MasterDeliveryWarehouse(
                row["id"], row["name"], row["address"], row["status"] == "active", row["sort_order"]
            )
            for row in rows
        ]

    def add_delivery_warehouse(self, name: str, address: str) -> int:
        normalized_name = name.strip()
        normalized_address = address.strip()
        if not normalized_name:
            raise ValueError("送货仓库名称不能为空")
        self._reject_duplicate("delivery_warehouses", "name", normalized_name)
        timestamp = _current_timestamp()
        with self._database.transaction() as connection:
            order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM delivery_warehouses"
            ).fetchone()[0]
            cursor = connection.execute(
                """
                INSERT INTO delivery_warehouses(name, address, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (normalized_name, normalized_address, order, timestamp, timestamp),
            )
            warehouse_id = cursor.lastrowid
        if warehouse_id is None:
            raise RuntimeError("delivery warehouse insert did not return an ID")
        return warehouse_id

    def update_delivery_warehouse(
        self, warehouse_id: int, *, name: str | None = None, address: str | None = None
    ) -> None:
        assignments: list[str] = []
        params: list[object] = []
        if name is not None:
            normalized_name = name.strip()
            if not normalized_name:
                raise ValueError("送货仓库名称不能为空")
            self._reject_duplicate(
                "delivery_warehouses",
                "name",
                normalized_name,
                exclude_column="id",
                exclude_value=warehouse_id,
            )
            assignments.append("name = ?")
            params.append(normalized_name)
        if address is not None:
            normalized_address = address.strip()
            assignments.append("address = ?")
            params.append(normalized_address)
        if not assignments:
            return
        assignments.append("updated_at = ?")
        params.append(_current_timestamp())
        params.append(warehouse_id)
        with self._database.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE delivery_warehouses SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            if cursor.rowcount == 0:
                raise KeyError(warehouse_id)

    def set_delivery_warehouse_active(self, warehouse_id: int, active: bool) -> None:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE delivery_warehouses SET status = ?, updated_at = ? WHERE id = ?",
                ("active" if active else "inactive", _current_timestamp(), warehouse_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(warehouse_id)

    # -- FBA warehouses ---------------------------------------------------

    def list_fba_warehouses(self, *, active_only: bool = False) -> list[MasterFbaWarehouse]:
        query = "SELECT code, name, address, status FROM fba_warehouses"
        if active_only:
            query += " WHERE status = 'active'"
        query += " ORDER BY code"
        connection = self._database.connect()
        try:
            rows = connection.execute(query).fetchall()
        finally:
            connection.close()
        return [
            MasterFbaWarehouse(row["code"], row["name"], row["address"], row["status"] == "active")
            for row in rows
        ]

    def get_fba_warehouse(self, code: str) -> MasterFbaWarehouse | None:
        normalized_code = canonical_amazon_warehouse_code(code)
        connection = self._database.connect()
        try:
            row = connection.execute(
                "SELECT code, name, address, status FROM fba_warehouses WHERE code = ?",
                (normalized_code,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return MasterFbaWarehouse(
            row["code"], row["name"], row["address"], row["status"] == "active"
        )

    def update_fba_warehouse(
        self, code: str, *, name: str | None = None, address: str | None = None
    ) -> None:
        normalized_code = canonical_amazon_warehouse_code(code)
        assignments: list[str] = []
        params: list[object] = []
        if name is not None:
            assignments.append("name = ?")
            params.append(name.strip())
        if address is not None:
            normalized_address = address.strip()
            if not normalized_address:
                raise ValueError("FBA 仓库地址不能为空")
            assignments.append("address = ?")
            params.append(normalized_address)
        if not assignments:
            return
        assignments.append("updated_at = ?")
        params.append(_current_timestamp())
        params.append(normalized_code)
        with self._database.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE fba_warehouses SET {', '.join(assignments)} WHERE code = ?",
                params,
            )
            if cursor.rowcount == 0:
                raise KeyError(normalized_code)

    def set_fba_warehouse_active(self, code: str, active: bool) -> None:
        normalized_code = canonical_amazon_warehouse_code(code)
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE fba_warehouses SET status = ?, updated_at = ? WHERE code = ?",
                ("active" if active else "inactive", _current_timestamp(), normalized_code),
            )
            if cursor.rowcount == 0:
                raise KeyError(normalized_code)


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
    "HistoryEntry",
    "MasterChannel",
    "MasterDataRepository",
    "MasterDeliveryWarehouse",
    "MasterFbaWarehouse",
    "ShipmentHistory",
]
