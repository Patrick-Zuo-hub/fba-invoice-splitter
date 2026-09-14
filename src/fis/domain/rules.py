from datetime import date, timedelta

_AMAZON_WAREHOUSE_PREFIX = "AMAZON-"


def canonical_amazon_warehouse_code(value: str) -> str:
    """Normalize short and prefixed warehouse codes to the master-data format."""

    normalized = value.strip().upper()
    if not normalized or normalized.startswith(_AMAZON_WAREHOUSE_PREFIX):
        return normalized
    return f"{_AMAZON_WAREHOUSE_PREFIX}{normalized}"


def default_dates(today: date) -> tuple[date, date]:
    """Pickup on the coming Thursday, sailing on the first Monday after it.

    Both are anchored to the pickup date rather than to today. Anchored to
    today, sailing would fall before pickup for anyone opening the tool from
    Friday onwards, when pickup has already rolled to the following week.
    """

    days_until_thursday = (3 - today.weekday()) % 7
    delivery = today + timedelta(days=days_until_thursday)
    # Computed rather than a fixed +4 so it stays correct if pickup ever moves
    # off Thursday; `or 7` keeps it strictly after a pickup that is a Monday.
    days_until_monday = (0 - delivery.weekday()) % 7 or 7
    return delivery, delivery + timedelta(days=days_until_monday)


def parse_warehouse_mapping(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    first_nonblank_row = True
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        columns = [part.strip() for part in raw_line.split("\t")]
        is_header = (
            first_nonblank_row
            and len(columns) == 2
            and all(columns)
            and columns[0].lower().replace(" ", "")
            in {"warehouse", "warehousecode", "仓库", "仓库代码"}
            and columns[1].lower().replace(" ", "")
            in {"shipment", "shipmentid", "货件编号", "货件单号"}
        )
        first_nonblank_row = False
        if is_header:
            continue
        if len(columns) != 2 or not all(columns):
            raise ValueError(f"Invalid mapping row {line_number}")
        code, shipment_id = canonical_amazon_warehouse_code(columns[0]), columns[1]
        if shipment_id in result and result[shipment_id] != code:
            raise ValueError(f"Conflicting warehouse mapping for {shipment_id}")
        result[shipment_id] = code
    return result
