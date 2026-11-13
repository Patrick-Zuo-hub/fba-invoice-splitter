"""Editable Equator state; provider dictionaries stay separate from v1 masters."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fis.domain.rules import default_dates
from fis.storage.repositories import DefaultsRepository
from fis.xlsx.equator import default_catalog
from fis.xlsx.parser import ParsedBundle

CUSTOMER = "深圳博致咨询有限公司"
EDITABLE = frozenset({"channel", "delivery_warehouse", "arrival_time", "customs"})
COMMON = frozenset({"customer_code", "remarks", "goods_material"})
ADDRESS_COLUMNS = dict(zip("KLMNOPQR", "BHIDEFJG", strict=True))


class EquatorState:
    def __init__(self, bundle: ParsedBundle, defaults: DefaultsRepository | None, today: date) -> None:
        self.bundle = bundle
        self.defaults = defaults
        stored = defaults.get("equator_catalog") if defaults else None
        self.catalog = json.loads(stored) if stored else default_catalog()
        self.channels = {c["name"]: c["code"] for c in self.catalog["channels"]}
        self.warehouses = {w["A"].upper(): w for w in self.catalog["warehouses"]}
        self.settings = {"customer_code": CUSTOMER, "remarks": "", "goods_material": "不涉检"}
        first_item = bundle.shipments[0].items[0].cells
        self.settings["remarks"] = first_item.get("C", "")
        self.settings["goods_material"] = first_item.get("D", "") or "不涉检"
        self.rows: dict[str, dict[str, str]] = {}
        self.explicit_channels: set[str] = set()
        default_arrival = default_dates(today)[0].isoformat() + "T00:00"
        for shipment in bundle.shipments:
            first = shipment.items[0].cells
            candidates = {item.cells.get("J", "").strip().upper().removeprefix("AMAZON-") for item in shipment.items}
            code = next(iter(candidates)) if len(candidates) == 1 else ""
            # A comma list is not a mapping. Never infer by list position.
            if code not in self.warehouses:
                code = ""
            channel = first.get("H", "")
            if channel not in self.channels:
                channel = next((n for n, c in self.channels.items() if c == first.get("G")), "")
            if channel:
                self.explicit_channels.add(shipment.shipment_id)
            if not channel and defaults and code:
                remembered = defaults.get("equator_channel:" + code)
                if remembered in self.channels:
                    channel = remembered or ""
            arrival = self._source_arrival(first.get("F", "")) or default_arrival
            domestic = first.get("E", "")
            self.rows[shipment.shipment_id] = {
                "warehouse_code": code, "channel": channel,
                "delivery_warehouse": domestic if domestic in self.catalog["domestic"] else "",
                "arrival_time": arrival, "customs": first.get("W", "") or "否",
            }

    @staticmethod
    def _source_arrival(value: str) -> str:
        if not value:
            return ""
        try:
            stamp = datetime(1899, 12, 30, tzinfo=UTC).replace(tzinfo=None) + timedelta(days=float(value))
        except (ValueError, OverflowError):
            try:
                stamp = datetime.fromisoformat(value)
            except ValueError:
                return ""
        return stamp.isoformat(timespec="minutes") if stamp.tzinfo is None else ""

    def fields(self, shipment_id: str) -> dict[str, str]:
        row = self.rows[shipment_id]
        result = {**row, **self.settings}
        result["channel_code"] = self.channels.get(row["channel"], "")
        warehouse = self.warehouses.get(row["warehouse_code"], {})
        for target, source in ADDRESS_COLUMNS.items():
            result["address_" + target] = warehouse.get(source, "")
        result["address_S"] = ""
        result["warehouse_address"] = ", ".join(warehouse.get(c, "") for c in ("G", "I", "H", "J", "B") if warehouse.get(c))
        return result

    def errors(self, shipment_id: str) -> dict[str, str]:
        f = self.fields(shipment_id)
        errors = {k: label + "必填" for k, label in (
            ("warehouse_code", "FBA 仓库"), ("channel", "物流渠道"),
            ("delivery_warehouse", "国内收货仓"), ("arrival_time", "预计到仓时间"),
            ("customer_code", "客户编码"),
        ) if not f[k]}
        if f["warehouse_code"] and not f["warehouse_address"]:
            errors["warehouse_address"] = "仓库未匹配到地址"
        if f["customs"] not in {"是", "否", "/"}:
            errors["customs"] = "请选择有效报关方式"
        if f["customs"] == "否" and not f["goods_material"]:
            errors["goods_material"] = "不报关时货物材质必填"
        for item in next(s for s in self.bundle.shipments if s.shipment_id == shipment_id).items:
            if f.get("address_K") in {"GB", "DE", "FR", "IT", "ES", "NL", "BE", "PL", "CZ", "CA", "AT", "SE", "FI", "DK", "IE", "PT", "RO", "HU", "SK", "SI", "HR", "GR", "BG", "LT", "LV", "EE", "LU", "MT", "CY", "NO", "CH", "IS"} and not item.cells.get("AT"):
                errors["sales_link"] = f"源文件 AT{item.source_row} 缺少销售链接"
        if "递延" in f["channel"]:
            for item in next(s for s in self.bundle.shipments if s.shipment_id == shipment_id).items:
                if not item.cells.get("AR") or not item.cells.get("AS"):
                    errors["tax_number"] = f"源文件第 {item.source_row} 行递延渠道需要 VAT 和 EORI"
        return errors

    def row_view(self, shipment_id: str) -> dict[str, Any]:
        shipment = next(s for s in self.bundle.shipments if s.shipment_id == shipment_id)
        names = list(dict.fromkeys(i.cells.get("AD", "") for i in shipment.items))
        errors = self.errors(shipment_id)
        return {
            "shipment_id": shipment_id, "fields": self.fields(shipment_id),
            "carton_count": str(sum((Decimal(i.cells.get("X") or "0") for i in shipment.items), Decimal(0))),
            "product_summary": "、".join(names[:2]) + (f"等 {len(names)} 种产品" if len(names) > 2 else ""),
            "item_count": len(shipment.items), "errors": errors, "complete": not errors,
            "items": [{"row": i.source_row, "name": i.cells.get("AD"), "cartons": i.cells.get("X"), "quantity": i.cells.get("AK"), "amount": i.cells.get("AM")} for i in shipment.items],
        }

    def save(self, shipment_id: str, field: str, value: object) -> None:
        if field not in EDITABLE:
            raise ValueError("该字段不支持编辑，请通过映射设置 FBA 仓库")
        text = str(value or "").strip()
        if text:
            if field == "channel" and text not in self.channels:
                raise ValueError("请选择赤道字典中的物流渠道")
            if field == "delivery_warehouse" and text not in self.catalog["domestic"]:
                raise ValueError("请选择赤道字典中的国内收货仓")
            if field == "customs" and text not in {"是", "否", "/"}:
                raise ValueError("请选择有效报关方式")
            if field == "arrival_time":
                try:
                    parsed = datetime.fromisoformat(text)
                except ValueError as exc:
                    raise ValueError("请选择有效日期与时间") from exc
                if parsed.tzinfo is not None or "T" not in text:
                    raise ValueError("请输入本地日期与时间")
        self.rows[shipment_id][field] = text
        if field == "channel":
            if text:
                self.explicit_channels.add(shipment_id)
            else:
                self.explicit_channels.discard(shipment_id)
        if field == "channel" and self.defaults and self.rows[shipment_id]["warehouse_code"]:
            self.defaults.set("equator_channel:" + self.rows[shipment_id]["warehouse_code"], text)

    def map_warehouse(self, shipment_id: str, code: str) -> None:
        code = code.removeprefix("AMAZON-")
        previous = self.rows[shipment_id]["warehouse_code"]
        self.rows[shipment_id]["warehouse_code"] = code
        # Preserve the user's explicit current-batch choice when remapping.
        if shipment_id not in self.explicit_channels and previous != code:
            self.rows[shipment_id]["channel"] = ""
            remembered = self.defaults.get("equator_channel:" + code) if self.defaults else None
            if remembered in self.channels:
                self.rows[shipment_id]["channel"] = remembered or ""

    def save_settings(self, changes: Mapping[str, object]) -> None:
        if any(k not in COMMON for k in changes):
            raise ValueError("不支持的公共字段")
        for key, value in changes.items():
            self.settings[key] = str(value or "").strip()

    def view(self, batch_id: str, output_directory: str | None) -> dict[str, Any]:
        rows = [self.row_view(s.shipment_id) for s in self.bundle.shipments]
        return {
            "equator": True, "batch_id": batch_id, "rows": rows,
            "complete_count": sum(r["complete"] for r in rows), "total_count": len(rows),
            "item_count": len(self.bundle.product_rows), "settings": self.settings,
            "channel_choices": tuple(enumerate(self.channels, 1)), "channel_codes": self.channels,
            "delivery_warehouse_choices": tuple(enumerate(self.catalog["domestic"], 1)),
            "output_directory": output_directory, "common_errors": {},
        }
