from datetime import date, timedelta

import pytest

from fis.domain.rules import (
    canonical_amazon_warehouse_code,
    declaration_currency,
    default_dates,
    parse_warehouse_mapping,
    sailing_after,
)


@pytest.mark.parametrize("country,currency", [
    ("US", "USD"), ("GB", "GBP"), ("CA", "CAD"), ("DE", "EUR"),
    ("FR", "EUR"), ("IT", "EUR"), ("ES", "EUR"), ("NL", "EUR"),
    ("BG", "EUR"), ("HR", "EUR"), (" gb ", "GBP"),
    ("PL", None), ("CZ", None), ("SE", None), ("JP", None),
    ("AU", None), ("MX", None), ("CN", None), ("", None), ("ZZ", None),
])
def test_declaration_currency_only_uses_confirmed_country_rules(
    country: str, currency: str | None,
) -> None:
    assert declaration_currency(country) == currency


def test_pickup_is_the_thursday_strictly_after_today() -> None:
    """On a Thursday the cut-off has passed, so pickup is the following week."""

    assert default_dates(date(2026, 9, 14))[0] == date(2026, 9, 17)  # Monday
    assert default_dates(date(2026, 9, 17))[0] == date(2026, 9, 24)  # Thursday rolls on
    assert default_dates(date(2026, 9, 18))[0] == date(2026, 9, 24)  # Friday


def test_sailing_is_the_first_monday_after_pickup() -> None:
    assert default_dates(date(2026, 9, 14)) == (date(2026, 9, 17), date(2026, 9, 21))
    assert default_dates(date(2026, 9, 17)) == (date(2026, 9, 24), date(2026, 9, 28))
    assert default_dates(date(2026, 9, 18)) == (date(2026, 9, 24), date(2026, 9, 28))


def test_sailing_always_follows_pickup_and_lands_on_a_monday() -> None:
    """Anchored to pickup, not to today: anchoring to today puts sailing before
    pickup for anyone opening the tool late in the week."""

    for offset in range(400):
        today = date(2026, 9, 7) + timedelta(days=offset)
        delivery, sailing = default_dates(today)
        assert delivery.weekday() == 3, delivery
        assert sailing.weekday() == 0, sailing
        assert delivery > today, (today, delivery)
        assert sailing > delivery


def test_mapping_trims_and_uppercases_codes() -> None:
    assert parse_warehouse_mapping("Warehouse Code\tShipment ID\n ont8 \t S1 \n") == {
        "S1": "AMAZON-ONT8"
    }


def test_amazon_code_accepts_prefixed_and_short_forms() -> None:
    assert canonical_amazon_warehouse_code(" ont8 ") == "AMAZON-ONT8"
    assert canonical_amazon_warehouse_code(" amazon-ont8 ") == "AMAZON-ONT8"


def test_mapping_rejects_conflicts() -> None:
    with pytest.raises(ValueError, match="S1"):
        parse_warehouse_mapping("ONT8\tS1\nLAS1\tS1")


def test_mapping_ignores_blank_lines() -> None:
    assert parse_warehouse_mapping("ONT8\tS1\n\n   \nLAS1\tS2") == {
        "S1": "AMAZON-ONT8",
        "S2": "AMAZON-LAS1",
    }


def test_mapping_allows_identical_duplicate_rows() -> None:
    assert parse_warehouse_mapping("ONT8\tS1\namazon-ont8\tS1") == {
        "S1": "AMAZON-ONT8"
    }


def test_mapping_rejects_malformed_column_count() -> None:
    with pytest.raises(ValueError, match="Invalid mapping row 1"):
        parse_warehouse_mapping("ONT8\tS1\textra")


def test_mapping_accepts_chinese_header() -> None:
    assert parse_warehouse_mapping("仓库代码\t货件单号\nONT8\tS1") == {
        "S1": "AMAZON-ONT8"
    }


def test_mapping_accepts_english_header_after_leading_blank_lines() -> None:
    assert parse_warehouse_mapping("\nWarehouse Code\tShipment ID\nONT8\tS1") == {
        "S1": "AMAZON-ONT8"
    }


def test_mapping_accepts_chinese_header_after_leading_blank_lines() -> None:
    assert parse_warehouse_mapping(" \n仓库代码\t货件单号\nONT8\tS1") == {
        "S1": "AMAZON-ONT8"
    }


def test_mapping_rejects_malformed_recognized_header() -> None:
    with pytest.raises(ValueError, match="Invalid mapping row 1"):
        parse_warehouse_mapping("Warehouse Code\tShipment ID\tExtra")


def test_sailing_after_is_the_rule_on_its_own() -> None:
    """The workspace needs this separately: when a user edits the pickup date,
    sailing has to follow that date rather than today's default."""

    assert sailing_after(date(2026, 9, 17)) == date(2026, 9, 21)  # Thursday
    assert sailing_after(date(2026, 9, 21)) == date(2026, 9, 28)  # Monday moves on
    assert sailing_after(date(2026, 9, 20)) == date(2026, 9, 21)  # Sunday
    for offset in range(400):
        pickup = date(2026, 9, 7) + timedelta(days=offset)
        sailing = sailing_after(pickup)
        assert sailing.weekday() == 0, sailing
        assert sailing > pickup


def test_default_dates_uses_the_same_rule() -> None:
    """Stated once. It was written out three times and a change to one of them
    left the other two behind."""

    for offset in range(400):
        delivery, sailing = default_dates(date(2026, 9, 7) + timedelta(days=offset))
        assert sailing == sailing_after(delivery)
