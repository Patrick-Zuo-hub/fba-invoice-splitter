"""Immutable contracts for supported invoice workbook templates."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class TemplateConfig:
    """Description of the worksheet layout used by an invoice template.

    The mapping fields are copied into ``mappingproxy`` objects during
    construction so that ``frozen=True`` also protects nested configuration.
    """

    type: str
    version: str
    sheet_name: str
    product_start_row: int
    value_ranges: Mapping[str, str]
    total_columns: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "value_ranges", MappingProxyType(dict(self.value_ranges)))
        object.__setattr__(self, "total_columns", MappingProxyType(dict(self.total_columns)))


STANDARD_V1 = TemplateConfig(
    type="fba-customs-standard",
    version="v1",
    sheet_name="装箱单&发票 发货信息",
    product_start_row=12,
    value_ranges={
        "warehouse_code": "C4:F4",
        "warehouse_address": "C5:F5",
        "shipping_channel": "C6:F6",
        "delivery_date": "C8:D8",
        "sailing_date": "C9:D9",
        "delivery_warehouse": "C10:D10",
    },
    total_columns={
        "cartons": "K",
        "gross_weight": "M",
        "volume": "N",
        "amount": "P",
    },
)
