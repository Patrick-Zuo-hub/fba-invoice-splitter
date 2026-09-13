from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from fis.storage.database import Database
from fis.storage.repositories import MasterDataRepository
from fis.xlsx.masters import MasterData


@pytest.fixture
def repository(tmp_path: Path) -> MasterDataRepository:
    database = Database(tmp_path / "fis.db")
    database.initialize()
    return MasterDataRepository(database)


def _data(
    *,
    warehouses: tuple[tuple[str, str], ...] = (),
    channels: tuple[str, ...] = (),
    delivery: tuple[str, ...] = (),
) -> MasterData:
    return MasterData(
        fba_warehouses=warehouses, channels=channels, delivery_warehouses=delivery
    )


def test_import_adds_everything_to_an_empty_database(
    repository: MasterDataRepository,
) -> None:
    summary = repository.import_master_data(
        _data(
            warehouses=(("AMAZON-ONT8", "Ontario"), ("AMAZON-IAH3", "Houston")),
            channels=("海运", "空运"),
            delivery=("义乌仓",),
        )
    )

    assert (summary.fba_warehouses.added, summary.fba_warehouses.updated) == (2, 0)
    assert (summary.channels.added, summary.channels.updated) == (2, 0)
    assert (summary.delivery_warehouses.added, summary.delivery_warehouses.updated) == (1, 0)
    assert [w.code for w in repository.list_fba_warehouses()] == ["AMAZON-IAH3", "AMAZON-ONT8"]


def test_the_file_wins_when_an_address_differs(repository: MasterDataRepository) -> None:
    repository.create_fba_warehouse("ONT8", "Stale address")

    summary = repository.import_master_data(
        warehouses_data := _data(warehouses=(("AMAZON-ONT8", "Fresh address"),))
    )

    assert warehouses_data.fba_warehouses[0][1] == "Fresh address"
    assert (summary.fba_warehouses.added, summary.fba_warehouses.updated) == (0, 1)
    warehouse = repository.get_fba_warehouse("AMAZON-ONT8")
    assert warehouse is not None and warehouse.address == "Fresh address"


def test_an_unchanged_row_counts_as_neither_added_nor_updated(
    repository: MasterDataRepository,
) -> None:
    repository.create_fba_warehouse("ONT8", "Ontario")
    repository.add_channel("海运")

    summary = repository.import_master_data(
        _data(warehouses=(("AMAZON-ONT8", "Ontario"),), channels=("海运",))
    )

    assert (summary.fba_warehouses.added, summary.fba_warehouses.updated) == (0, 0)
    assert (summary.channels.added, summary.channels.updated) == (0, 0)


def test_entries_absent_from_the_file_are_left_alone(
    repository: MasterDataRepository,
) -> None:
    """History references channels and warehouses by name; deleting orphans it."""

    repository.create_fba_warehouse("LAX9", "Los Angeles")
    repository.add_channel("美森正班")

    repository.import_master_data(
        _data(warehouses=(("AMAZON-ONT8", "Ontario"),), channels=("海运",))
    )

    assert {w.code for w in repository.list_fba_warehouses()} == {"AMAZON-LAX9", "AMAZON-ONT8"}
    assert {c.name for c in repository.list_channels()} == {"美森正班", "海运"}


def test_import_never_blanks_an_existing_delivery_address(
    repository: MasterDataRepository,
) -> None:
    """The sheet carries names only, so it must not be read as 'address cleared'."""

    repository.add_delivery_warehouse("义乌仓", "浙江省义乌市测试路 1 号")

    repository.import_master_data(_data(delivery=("义乌仓",)))

    saved = [w for w in repository.list_delivery_warehouses() if w.name == "义乌仓"]
    assert saved and saved[0].address == "浙江省义乌市测试路 1 号"


def test_import_leaves_a_deactivated_entry_deactivated(
    repository: MasterDataRepository,
) -> None:
    """Deactivation is a deliberate local action; a re-import must not undo it."""

    channel_id = repository.add_channel("空运")
    repository.set_channel_active(channel_id, False)

    repository.import_master_data(_data(channels=("空运",)))

    assert [c.active for c in repository.list_channels() if c.name == "空运"] == [False]


def test_a_failed_row_rolls_the_whole_import_back(
    repository: MasterDataRepository, tmp_path: Path
) -> None:
    repository.create_fba_warehouse("ONT8", "Ontario")

    with pytest.raises(ValueError):
        repository.import_master_data(
            _data(
                warehouses=(("AMAZON-ONT8", "Fresh address"), ("", "no code")),
                channels=("海运",),
            )
        )

    warehouse = repository.get_fba_warehouse("AMAZON-ONT8")
    assert warehouse is not None and warehouse.address == "Ontario"
    assert repository.list_channels() == []
    with closing(sqlite3.connect(tmp_path / "fis.db")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM fba_warehouses").fetchone()[0] == 1
