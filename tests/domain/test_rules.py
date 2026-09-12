from datetime import date

import pytest

from fis.domain.rules import (
    canonical_amazon_warehouse_code,
    default_dates,
    parse_warehouse_mapping,
)


def test_default_date_uses_nearest_thursday() -> None:
    assert default_dates(date(2026, 9, 7)) == (date(2026, 9, 10), date(2026, 9, 17))
    assert default_dates(date(2026, 9, 10)) == (date(2026, 9, 10), date(2026, 9, 17))


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
