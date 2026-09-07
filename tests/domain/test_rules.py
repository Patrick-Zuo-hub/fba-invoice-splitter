from datetime import date

import pytest

from fis.domain.rules import default_dates, parse_warehouse_mapping


def test_default_date_uses_nearest_thursday() -> None:
    assert default_dates(date(2026, 9, 7)) == (date(2026, 9, 10), date(2026, 9, 17))
    assert default_dates(date(2026, 9, 10)) == (date(2026, 9, 10), date(2026, 9, 17))


def test_mapping_trims_and_uppercases_codes() -> None:
    assert parse_warehouse_mapping("Shipment ID\tWarehouse\n S1 \t ont8 \n") == {
        "S1": "ONT8"
    }


def test_mapping_rejects_conflicts() -> None:
    with pytest.raises(ValueError, match="S1"):
        parse_warehouse_mapping("S1\tONT8\nS1\tLAS1")


def test_mapping_ignores_blank_lines() -> None:
    assert parse_warehouse_mapping("S1\tONT8\n\n   \nS2\tLAS1") == {
        "S1": "ONT8",
        "S2": "LAS1",
    }


def test_mapping_allows_identical_duplicate_rows() -> None:
    assert parse_warehouse_mapping("S1\tONT8\nS1\tont8") == {"S1": "ONT8"}


def test_mapping_rejects_malformed_column_count() -> None:
    with pytest.raises(ValueError, match="Invalid mapping row 1"):
        parse_warehouse_mapping("S1\tONT8\textra")


def test_mapping_accepts_chinese_header() -> None:
    assert parse_warehouse_mapping("货件编号\t仓库\nS1\tONT8") == {"S1": "ONT8"}


def test_mapping_accepts_english_header_after_leading_blank_lines() -> None:
    assert parse_warehouse_mapping("\nShipment ID\tWarehouse\nS1\tONT8") == {"S1": "ONT8"}


def test_mapping_accepts_chinese_header_after_leading_blank_lines() -> None:
    assert parse_warehouse_mapping(" \n货件编号\t仓库\nS1\tONT8") == {"S1": "ONT8"}


def test_mapping_rejects_malformed_recognized_header() -> None:
    with pytest.raises(ValueError, match="Invalid mapping row 1"):
        parse_warehouse_mapping("Shipment ID\tWarehouse\tExtra")
