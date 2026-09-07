# FIS v1 Foundation and Local Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the tested Python package, domain rules, SQLite schema, repositories, and safe per-batch filesystem used by every later FIS subsystem.

**Architecture:** Keep domain rules independent from FastAPI and OOXML. Store metadata and immutable successful-history snapshots in SQLite, while source and generated files live under a validated application data root. Use explicit transactions for state changes and filesystem publication.

**Tech Stack:** Python 3.12, Pydantic 2, SQLite, pytest, Ruff, mypy

---

## File map

| Path | Responsibility |
| --- | --- |
| `pyproject.toml` | Runtime/development dependencies and tool configuration |
| `src/fis/config.py` | Application paths and runtime settings |
| `src/fis/domain/models.py` | Batch, Shipment, and master-data types |
| `src/fis/domain/rules.py` | Mapping normalization and date/default rules |
| `src/fis/storage/schema.py` | Versioned SQLite migrations |
| `src/fis/storage/database.py` | Connections and transaction boundary |
| `src/fis/storage/repositories.py` | Batch, Shipment, and master-data persistence |
| `src/fis/storage/batch_files.py` | Safe batch directories, staging, publication, deletion |
| `src/fis/storage/recovery.py` | Interrupted-batch recovery |
| `tests/` | Unit and integration tests matching each module |

### Task 1: Bootstrap the Python package

**Files:**
- Create: `pyproject.toml`
- Create: `src/fis/__init__.py`
- Create: `tests/test_package.py`
- Delete: `src/.gitkeep`
- Delete: `tests/.gitkeep`

- [ ] **Step 1: Write the failing package test**

```python
from fis import __version__


def test_package_version() -> None:
    assert __version__ == "0.1.0"
```

- [ ] **Step 2: Run the test and verify the package is missing**

Run: `python -m pytest tests/test_package.py -v`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'fis'`.

- [ ] **Step 3: Add project metadata and the minimal package**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "fba-invoice-splitter"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "fastapi>=0.115,<1",
  "jinja2>=3.1,<4",
  "lxml>=5.3,<6",
  "python-multipart>=0.0.20,<1",
  "pydantic>=2.10,<3",
  "uvicorn>=0.34,<1",
]

[project.optional-dependencies]
dev = [
  "httpx>=0.28,<1",
  "mypy>=1.14,<2",
  "playwright>=1.50,<2",
  "pyinstaller>=6.11,<7",
  "pytest>=8.3,<9",
  "pytest-asyncio>=0.25,<1",
  "ruff>=0.9,<1",
]

[tool.hatch.build.targets.wheel]
packages = ["src/fis"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.mypy]
python_version = "3.12"
strict = true
packages = ["fis"]
```

```python
__version__ = "0.1.0"
```

- [ ] **Step 4: Install development dependencies and run the test**

Run: `python -m pip install -e '.[dev]'`

Expected: installation succeeds.

Run: `python -m pytest tests/test_package.py -v`

Expected: PASS.

- [ ] **Step 5: Run static checks and commit**

Run: `python -m ruff check src tests && python -m mypy src`

Expected: both commands exit 0.

```bash
git add pyproject.toml src/fis/__init__.py tests/test_package.py src/.gitkeep tests/.gitkeep
git commit -m "build: bootstrap FIS Python package"
```

### Task 2: Define settings and domain models

**Files:**
- Create: `src/fis/config.py`
- Create: `src/fis/domain/__init__.py`
- Create: `src/fis/domain/models.py`
- Test: `tests/domain/test_models.py`

- [ ] **Step 1: Write failing tests for paths, states, and immutable snapshots**

```python
from datetime import date
from pathlib import Path

import pytest

from fis.config import Settings
from fis.domain.models import BatchState, ShipmentSnapshot


def test_settings_resolve_data_children(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "data")
    assert settings.database_path == tmp_path / "data" / "fis.db"
    assert settings.batches_root == tmp_path / "data" / "batches"


def test_completed_state_is_terminal() -> None:
    assert BatchState.COMPLETED.is_terminal is True
    assert BatchState.GENERATING.is_terminal is False


def test_snapshot_rejects_blank_shipment_id() -> None:
    with pytest.raises(ValueError, match="Shipment ID"):
        ShipmentSnapshot(
            shipment_id=" ", warehouse_code="ONT8", warehouse_address="Address",
            channel_name="Ocean", delivery_date=date(2026, 9, 10),
            sailing_date=date(2026, 9, 17), delivery_warehouse_name="Yiwu",
            delivery_warehouse_address="Address", chinese_names=("产品",),
        )
```

- [ ] **Step 2: Run the tests and verify imports fail**

Run: `python -m pytest tests/domain/test_models.py -v`

Expected: FAIL with missing `fis.config` and `fis.domain.models`.

- [ ] **Step 3: Implement settings and models**

```python
# src/fis/config.py
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_root: Path

    @property
    def database_path(self) -> Path:
        return self.data_root / "fis.db"

    @property
    def batches_root(self) -> Path:
        return self.data_root / "batches"
```

```python
# src/fis/domain/models.py
from datetime import date
from enum import StrEnum
from pydantic import BaseModel, ConfigDict, field_validator


class BatchState(StrEnum):
    PARSING = "parsing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    GENERATING = "generating"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"

    @property
    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.INCOMPLETE}


class ShipmentSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    shipment_id: str
    warehouse_code: str
    warehouse_address: str
    channel_name: str
    delivery_date: date
    sailing_date: date
    delivery_warehouse_name: str
    delivery_warehouse_address: str
    chinese_names: tuple[str, ...]

    @field_validator("shipment_id")
    @classmethod
    def shipment_id_must_exist(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Shipment ID is required")
        return normalized
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/domain/test_models.py -v`

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/fis/config.py src/fis/domain tests/domain/test_models.py
git commit -m "feat: define FIS domain models"
```

### Task 3: Implement mapping and date rules

**Files:**
- Create: `src/fis/domain/rules.py`
- Test: `tests/domain/test_rules.py`

- [ ] **Step 1: Write failing rule tests**

```python
from datetime import date
import pytest
from fis.domain.rules import default_dates, parse_warehouse_mapping


def test_default_date_uses_nearest_thursday() -> None:
    assert default_dates(date(2026, 9, 7)) == (date(2026, 9, 10), date(2026, 9, 17))
    assert default_dates(date(2026, 9, 10)) == (date(2026, 9, 10), date(2026, 9, 17))


def test_mapping_trims_and_uppercases_codes() -> None:
    assert parse_warehouse_mapping("Shipment ID\tWarehouse\n S1 \t ont8 \n") == {"S1": "ONT8"}


def test_mapping_rejects_conflicts() -> None:
    with pytest.raises(ValueError, match="S1"):
        parse_warehouse_mapping("S1\tONT8\nS1\tLAS1")
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/domain/test_rules.py -v`

Expected: FAIL because `fis.domain.rules` does not exist.

- [ ] **Step 3: Implement the rules**

```python
from datetime import date, timedelta


def default_dates(today: date) -> tuple[date, date]:
    days_until_thursday = (3 - today.weekday()) % 7
    delivery = today + timedelta(days=days_until_thursday)
    return delivery, delivery + timedelta(days=7)


def parse_warehouse_mapping(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        columns = [part.strip() for part in raw_line.split("\t")]
        if line_number == 1 and columns[0].lower().replace(" ", "") in {"shipmentid", "货件编号"}:
            continue
        if len(columns) != 2 or not all(columns):
            raise ValueError(f"Invalid mapping row {line_number}")
        shipment_id, code = columns[0], columns[1].upper()
        if shipment_id in result and result[shipment_id] != code:
            raise ValueError(f"Conflicting warehouse mapping for {shipment_id}")
        result[shipment_id] = code
    return result
```

- [ ] **Step 4: Run rule and package tests**

Run: `python -m pytest tests/domain tests/test_package.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/fis/domain/rules.py tests/domain/test_rules.py
git commit -m "feat: add mapping and logistics defaults"
```

### Task 4: Add versioned SQLite migrations

**Files:**
- Create: `src/fis/storage/__init__.py`
- Create: `src/fis/storage/schema.py`
- Create: `src/fis/storage/database.py`
- Test: `tests/storage/test_database.py`

- [ ] **Step 1: Write the failing migration test**

```python
from pathlib import Path
from fis.storage.database import Database


def test_database_creates_v1_schema(tmp_path: Path) -> None:
    database = Database(tmp_path / "fis.db")
    database.initialize()
    with database.connect() as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert version == 1
    assert {"processing_batches", "shipments", "shipment_items", "fba_warehouses",
            "shipping_channels", "delivery_warehouses"} <= tables
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/storage/test_database.py -v`

Expected: FAIL because storage modules do not exist.

- [ ] **Step 3: Implement migration and transaction support**

```python
# src/fis/storage/schema.py
SCHEMA_V1 = """
PRAGMA foreign_keys = ON;
CREATE TABLE schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version(version) VALUES (1);
CREATE TABLE processing_batches(
  id TEXT PRIMARY KEY, source_filename TEXT NOT NULL, source_sha256 TEXT NOT NULL,
  template_type TEXT NOT NULL, template_version TEXT NOT NULL, state TEXT NOT NULL,
  shipment_count INTEGER NOT NULL DEFAULT 0, item_count INTEGER NOT NULL DEFAULT 0,
  source_path TEXT NOT NULL, zip_path TEXT, error_code TEXT, error_detail TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT
);
CREATE TABLE fba_warehouses(
  code TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', address TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', last_successful_channel_id INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE shipping_channels(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active', sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE delivery_warehouses(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, address TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE shipments(
  batch_id TEXT NOT NULL REFERENCES processing_batches(id) ON DELETE CASCADE,
  shipment_id TEXT NOT NULL, snapshot_json TEXT NOT NULL, sailing_manual INTEGER NOT NULL DEFAULT 0,
  output_path TEXT, PRIMARY KEY(batch_id, shipment_id)
);
CREATE TABLE shipment_items(
  batch_id TEXT NOT NULL, shipment_id TEXT NOT NULL, source_row INTEGER NOT NULL,
  fields_json TEXT NOT NULL, drawing_ids_json TEXT NOT NULL,
  PRIMARY KEY(batch_id, shipment_id, source_row),
  FOREIGN KEY(batch_id, shipment_id) REFERENCES shipments(batch_id, shipment_id) ON DELETE CASCADE
);
"""
```

```python
# src/fis/storage/database.py
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator
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
        with self.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if not exists:
                connection.executescript(SCHEMA_V1)

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
```

- [ ] **Step 4: Run the storage test**

Run: `python -m pytest tests/storage/test_database.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/fis/storage tests/storage/test_database.py
git commit -m "feat: add SQLite schema and transactions"
```

### Task 5: Implement batch and master-data repositories

**Files:**
- Create: `src/fis/storage/repositories.py`
- Test: `tests/storage/test_repositories.py`

- [ ] **Step 1: Write failing repository tests**

```python
from datetime import UTC, datetime
from pathlib import Path
from fis.domain.models import BatchState
from fis.storage.database import Database
from fis.storage.repositories import BatchRepository, MasterDataRepository


def test_only_completed_batches_appear_in_history(tmp_path: Path) -> None:
    database = Database(tmp_path / "fis.db"); database.initialize()
    repository = BatchRepository(database)
    repository.create("b1", "source.xlsx", "abc", "source/source.xlsx", datetime.now(UTC))
    assert repository.list_history() == []
    repository.set_state("b1", BatchState.COMPLETED, datetime.now(UTC))
    assert [batch.id for batch in repository.list_history()] == ["b1"]


def test_channel_default_updates_only_after_success(tmp_path: Path) -> None:
    database = Database(tmp_path / "fis.db"); database.initialize()
    masters = MasterDataRepository(database)
    channel_id = masters.create_channel("Ocean")
    masters.create_fba_warehouse("ONT8", "Ontario")
    masters.record_successful_channel("ONT8", channel_id)
    assert masters.get_recent_channel("ONT8").name == "Ocean"
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/storage/test_repositories.py -v`

Expected: FAIL because repositories are missing.

- [ ] **Step 3: Implement parameterized repositories**

Implement frozen row models and parameterized methods:

```python
from dataclasses import dataclass
from datetime import datetime
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


class BatchRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, batch_id: str, filename: str, sha256: str,
               source_path: str, now: datetime) -> None:
        timestamp = now.isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO processing_batches
                   (id, source_filename, source_sha256, template_type, template_version,
                    state, source_path, created_at, updated_at)
                   VALUES (?, ?, ?, 'fba-customs-standard', 'v1', ?, ?, ?, ?)""",
                (batch_id, filename, sha256, BatchState.PARSING.value,
                 source_path, timestamp, timestamp),
            )

    def set_state(self, batch_id: str, state: BatchState, now: datetime,
                  error_code: str | None = None, error_detail: str | None = None) -> None:
        completed_at = now.isoformat() if state is BatchState.COMPLETED else None
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE processing_batches SET state = ?, error_code = ?, error_detail = ?,
                   updated_at = ?, completed_at = COALESCE(?, completed_at) WHERE id = ?""",
                (state.value, error_code, error_detail, now.isoformat(), completed_at, batch_id),
            )

    def list_history(self) -> list[BatchRecord]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, state FROM processing_batches WHERE state = ? ORDER BY completed_at DESC",
                (BatchState.COMPLETED.value,),
            ).fetchall()
        return [BatchRecord(id=row["id"], state=BatchState(row["state"])) for row in rows]

    def get(self, batch_id: str) -> BatchRecord:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, state FROM processing_batches WHERE id = ?", (batch_id,),
            ).fetchone()
        if row is None:
            raise KeyError(batch_id)
        return BatchRecord(id=row["id"], state=BatchState(row["state"]))


class MasterDataRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_channel(self, name: str) -> int:
        now = datetime.now().astimezone().isoformat()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO shipping_channels(name, created_at, updated_at) VALUES (?, ?, ?)",
                (name.strip(), now, now),
            )
            return int(cursor.lastrowid)

    def create_fba_warehouse(self, code: str, address: str) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO fba_warehouses(code, address, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (code.strip().upper(), address.strip(), now, now),
            )

    def record_successful_channel(self, code: str, channel_id: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE fba_warehouses SET last_successful_channel_id = ? WHERE code = ?",
                (channel_id, code.strip().upper()),
            )

    def get_recent_channel(self, code: str) -> ChannelRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT c.id, c.name FROM fba_warehouses w
                   JOIN shipping_channels c ON c.id = w.last_successful_channel_id
                   WHERE w.code = ?""",
                (code.strip().upper(),),
            ).fetchone()
        return None if row is None else ChannelRecord(id=row["id"], name=row["name"])
```

`list_history()` must use `WHERE state = 'completed' ORDER BY completed_at DESC`. `record_successful_channel()` must be called only from the successful generation transaction; the repository must not infer success from a UI selection.

- [ ] **Step 4: Run tests and type checks**

Run: `python -m pytest tests/storage -v && python -m mypy src`

Expected: all tests pass and mypy exits 0.

- [ ] **Step 5: Commit**

```bash
git add src/fis/storage/repositories.py tests/storage/test_repositories.py
git commit -m "feat: persist batches and master data"
```

### Task 6: Add safe batch files and recovery

**Files:**
- Create: `src/fis/storage/batch_files.py`
- Create: `src/fis/storage/recovery.py`
- Test: `tests/storage/test_batch_files.py`
- Test: `tests/storage/test_recovery.py`

- [ ] **Step 1: Write failing path and publication tests**

```python
from pathlib import Path
import pytest
from fis.storage.batch_files import BatchFiles


def test_publish_replaces_no_existing_output(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")
    work = files.create("batch-1")
    (work / "invoice.xlsx").write_bytes(b"xlsx")
    output = files.publish("batch-1")
    assert (output / "invoice.xlsx").read_bytes() == b"xlsx"
    assert not work.exists()


def test_batch_id_cannot_escape_data_root(tmp_path: Path) -> None:
    files = BatchFiles(tmp_path / "data")
    with pytest.raises(ValueError, match="batch ID"):
        files.create("../outside")
```

- [ ] **Step 2: Verify failure**

Run: `python -m pytest tests/storage/test_batch_files.py -v`

Expected: FAIL because `BatchFiles` is missing.

- [ ] **Step 3: Implement safe paths and atomic publication**

```python
from pathlib import Path
import re

SAFE_ID = re.compile(r"^[a-f0-9-]{36}$|^batch-[a-z0-9-]+$")


class BatchFiles:
    def __init__(self, data_root: Path) -> None:
        self.batches_root = data_root.resolve() / "batches"

    def _batch(self, batch_id: str) -> Path:
        if not SAFE_ID.fullmatch(batch_id):
            raise ValueError("Invalid batch ID")
        path = (self.batches_root / batch_id).resolve()
        if self.batches_root not in path.parents:
            raise ValueError("Invalid batch ID")
        return path

    def create(self, batch_id: str) -> Path:
        work = self._batch(batch_id) / "work"
        work.mkdir(parents=True, exist_ok=False)
        return work

    def publish(self, batch_id: str) -> Path:
        batch = self._batch(batch_id)
        work, output = batch / "work", batch / "output"
        if output.exists() or not work.is_dir():
            raise FileExistsError("Batch output cannot be published")
        work.replace(output)
        return output
```

Add validated deletion and interrupted-state lookup:

```python
# additions to BatchFiles
import shutil

def work_path(self, batch_id: str) -> Path:
    return self._batch(batch_id) / "work"

def output_path(self, batch_id: str) -> Path:
    return self._batch(batch_id) / "output"

def clear_work(self, batch_id: str) -> None:
    work = self.work_path(batch_id)
    if work.is_symlink():
        raise ValueError("Symlinked batch work directory is not allowed")
    if work.exists():
        shutil.rmtree(work)

def clear_output(self, batch_id: str) -> None:
    output = self.output_path(batch_id)
    if output.is_symlink():
        raise ValueError("Symlinked batch output directory is not allowed")
    if output.exists():
        shutil.rmtree(output)

def delete_batch(self, batch_id: str) -> None:
    batch = self._batch(batch_id)
    if batch.is_symlink():
        raise ValueError("Symlinked batch directory is not allowed")
    if batch.exists():
        shutil.rmtree(batch)
```

```python
# repository additions and src/fis/storage/recovery.py
def list_interrupted(self) -> list[BatchRecord]:
    with self.database.connect() as connection:
        rows = connection.execute(
            "SELECT id, state FROM processing_batches WHERE state IN (?, ?)",
            (BatchState.PARSING.value, BatchState.GENERATING.value),
        ).fetchall()
    return [BatchRecord(id=row["id"], state=BatchState(row["state"])) for row in rows]


def recover_interrupted(repository: BatchRepository, files: BatchFiles, now: datetime) -> None:
    for batch in repository.list_interrupted():
        files.clear_work(batch.id)
        repository.set_state(
            batch.id, BatchState.INCOMPLETE, now,
            error_code="INTERRUPTED", error_detail="上次运行意外中断，可重新生成。",
        )
```

- [ ] **Step 4: Add and run recovery tests**

```python
def test_recovery_marks_generating_batch_incomplete(database, batch_files, repository) -> None:
    repository.create("batch-1", "source.xlsx", "abc", "source/source.xlsx", FIXED_NOW)
    repository.set_state("batch-1", BatchState.GENERATING, FIXED_NOW)
    batch_files.create("batch-1")
    recover_interrupted(repository, batch_files, FIXED_NOW)
    assert repository.get("batch-1").state is BatchState.INCOMPLETE
    assert not batch_files.work_path("batch-1").exists()
```

Run: `python -m pytest tests/storage -v`

Expected: all storage tests pass.

- [ ] **Step 5: Run the foundation suite and commit**

Run: `python -m pytest -v && python -m ruff check src tests && python -m mypy src`

Expected: all commands exit 0.

```bash
git add src/fis/storage tests/storage
git commit -m "feat: add atomic batch storage and recovery"
```

## Foundation completion checkpoint

Run: `python -m pytest -v && python -m ruff check src tests && python -m mypy src`

Expected: all tests pass; lint and type checks exit 0. Confirm no file outside the temporary test directories or configured `data_root` was created, modified, or deleted.
