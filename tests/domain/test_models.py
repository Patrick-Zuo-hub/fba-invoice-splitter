from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

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
            shipment_id=" ",
            warehouse_code="ONT8",
            warehouse_address="Address",
            channel_name="Ocean",
            delivery_date=date(2026, 9, 10),
            sailing_date=date(2026, 9, 17),
            delivery_warehouse_name="Yiwu",
            delivery_warehouse_address="Address",
            chinese_names=("产品",),
        )


def test_snapshot_trims_valid_shipment_id() -> None:
    snapshot = ShipmentSnapshot(
        shipment_id=" S1 ",
        warehouse_code="ONT8",
        warehouse_address="Address",
        channel_name="Ocean",
        delivery_date=date(2026, 9, 10),
        sailing_date=date(2026, 9, 17),
        delivery_warehouse_name="Yiwu",
        delivery_warehouse_address="Address",
        chinese_names=("产品",),
    )

    assert snapshot.shipment_id == "S1"


def test_settings_and_snapshot_are_frozen(tmp_path: Path) -> None:
    settings = Settings(data_root=tmp_path / "data")
    snapshot = ShipmentSnapshot(
        shipment_id="S1",
        warehouse_code="ONT8",
        warehouse_address="Address",
        channel_name="Ocean",
        delivery_date=date(2026, 9, 10),
        sailing_date=date(2026, 9, 17),
        delivery_warehouse_name="Yiwu",
        delivery_warehouse_address="Address",
        chinese_names=("产品",),
    )

    attribute_name = "data_root"
    with pytest.raises(FrozenInstanceError):
        setattr(settings, attribute_name, tmp_path)
    with pytest.raises(ValidationError):
        snapshot.shipment_id = "S2"
