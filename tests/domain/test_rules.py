from datetime import date, timedelta

import pytest

from fis.domain.rules import (
    canonical_amazon_warehouse_code,
    default_dates,
    parse_warehouse_mapping,
)


def test_pickup_is_the_next_thursday_and_today_counts() -> None:
    assert default_dates(date(2026, 9, 7))[0] == date(2026, 9, 10)   # Monday
    assert default_dates(date(2026, 9, 10))[0] == date(2026, 9, 10)  # Thursday itself
    assert default_dates(date(2026, 9, 11))[0] == date(2026, 9, 17)  # Friday rolls on


def test_sailing_is_the_first_monday_after_pickup() -> None:
    assert default_dates(date(2026, 9, 7)) == (date(2026, 9, 10), date(2026, 9, 14))
    assert default_dates(date(2026, 9, 10)) == (date(2026, 9, 10), date(2026, 9, 14))
    assert default_dates(date(2026, 9, 11)) == (date(2026, 9, 17), date(2026, 9, 21))


def test_sailing_always_follows_pickup_and_lands_on_a_monday() -> None:
    """Anchored to pickup, not to today: anchoring to today puts sailing before
    pickup for anyone opening the tool late in the week."""

    for offset in range(400):
        delivery, sailing = default_dates(date(2026, 9, 7) + timedelta(days=offset))
        assert delivery.weekday() == 3, delivery
        assert sailing.weekday() == 0, sailing
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
