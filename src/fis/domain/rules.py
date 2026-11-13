from datetime import date, timedelta

_AMAZON_WAREHOUSE_PREFIX = "AMAZON-"

# User-confirmed declaration mapping; no exchange-rate conversion is performed.
# Euro-area membership checked against ECB on 2026-11-13 (including BG).
_EURO_AREA_COUNTRIES = frozenset({
    "AT", "BE", "BG", "HR", "CY", "EE", "FI", "FR", "DE", "GR", "IE",
    "IT", "LV", "LT", "LU", "MT", "NL", "PT", "SK", "SI", "ES",
})


def declaration_currency(country: str) -> str | None:
    """Resolve only confirmed country rules, never silently fall back to USD."""

    code = country.strip().upper()
    if code in _EURO_AREA_COUNTRIES:
        return "EUR"
    return {"US": "USD", "GB": "GBP", "CA": "CAD"}.get(code)


def canonical_amazon_warehouse_code(value: str) -> str:
    """Normalize short and prefixed warehouse codes to the master-data format."""

    normalized = value.strip().upper()
    if not normalized or normalized.startswith(_AMAZON_WAREHOUSE_PREFIX):
        return normalized
    return f"{_AMAZON_WAREHOUSE_PREFIX}{normalized}"


def sailing_after(delivery: date) -> date:
    """The first Monday strictly after a pickup date.

    Stated here once because three places needed it: the defaults, the
    workspace view, and the follow-up when a user edits the pickup date. It
    had been written out at each of them, so changing one left the other two
    on the old rule.
    """

    return delivery + timedelta(days=(0 - delivery.weekday()) % 7 or 7)


def default_dates(today: date) -> tuple[date, date]:
    """Pickup on the next Thursday, sailing on the first Monday after it.

    Both dates are strictly in the future: on a Thursday that week's cut-off
    has passed, so pickup moves to the following week rather than landing on
    today.

    Sailing is anchored to the pickup date rather than to today. Anchored to
    today it would fall before pickup for anyone opening the tool late in the
    week, once pickup has already rolled forward.
    """

    # `or 7` is what keeps today off the list when today is a Thursday.
    days_until_thursday = (3 - today.weekday()) % 7 or 7
    delivery = today + timedelta(days=days_until_thursday)
    return delivery, sailing_after(delivery)


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
