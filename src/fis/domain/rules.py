from datetime import date, timedelta


def default_dates(today: date) -> tuple[date, date]:
    days_until_thursday = (3 - today.weekday()) % 7
    delivery = today + timedelta(days=days_until_thursday)
    return delivery, delivery + timedelta(days=7)


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
        code, shipment_id = columns[0].upper(), columns[1]
        if shipment_id in result and result[shipment_id] != code:
            raise ValueError(f"Conflicting warehouse mapping for {shipment_id}")
        result[shipment_id] = code
    return result
