from pathlib import Path

import pytest

from fis.storage.database import Database
from fis.storage.repositories import DefaultsRepository


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "fis.db")
    database.initialize()
    return database


@pytest.fixture
def defaults_repository(database: Database) -> DefaultsRepository:
    return DefaultsRepository(database)


def test_set_then_get_returns_value(defaults_repository: DefaultsRepository) -> None:
    defaults_repository.set("channel", "Ocean")

    assert defaults_repository.get("channel") == "Ocean"


def test_missing_default_is_none(defaults_repository: DefaultsRepository) -> None:
    assert defaults_repository.get("channel") is None


def test_set_overwrites_existing_value(defaults_repository: DefaultsRepository) -> None:
    defaults_repository.set("channel", "Ocean")
    defaults_repository.set("channel", "Air")

    assert defaults_repository.get("channel") == "Air"


def test_completed_value_is_available_after_database_reopen(
    database: Database, defaults_repository: DefaultsRepository
) -> None:
    defaults_repository.set("channel", "Ocean")

    assert DefaultsRepository(Database(database.path)).get("channel") == "Ocean"


def test_all_returns_every_stored_default(defaults_repository: DefaultsRepository) -> None:
    defaults_repository.set("channel", "Ocean")
    defaults_repository.set("delivery_warehouse", "宁波仓")

    assert defaults_repository.all() == {"channel": "Ocean", "delivery_warehouse": "宁波仓"}


def test_blank_key_is_rejected(defaults_repository: DefaultsRepository) -> None:
    with pytest.raises(ValueError):
        defaults_repository.set("   ", "Ocean")


def test_non_string_value_is_rejected(defaults_repository: DefaultsRepository) -> None:
    with pytest.raises(TypeError):
        defaults_repository.set("channel", b"Ocean")  # type: ignore[arg-type]
