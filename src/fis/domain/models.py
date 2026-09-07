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
