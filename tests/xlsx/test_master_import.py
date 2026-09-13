from __future__ import annotations

from pathlib import Path

import pytest

from fis.xlsx.errors import WorkbookError
from fis.xlsx.masters import parse_master_data
from tests.fixtures.master_data_builder import (
    CHANNEL_SHEET,
    DELIVERY_SHEET,
    FBA_SHEET,
    build_master_data,
)


def _complete(tmp_path: Path) -> Path:
    return build_master_data(
        tmp_path / "masters.xlsx",
        sheets={
            FBA_SHEET: [
                ["FBA仓代码", "FBA仓详细地址"],
                ["AMAZON-ONT8", "2245 Cactus Ave, Ontario, CA 91761"],
                ["IAH3", "10384 W Airport Blvd, Houston, TX 77048"],
            ],
            CHANNEL_SHEET: [["渠道"], ["海运"], ["空运"]],
            DELIVERY_SHEET: [["国内交货仓"], ["义乌仓"], ["宁波仓"]],
        },
    )


def test_parses_every_sheet_into_typed_rows(tmp_path: Path) -> None:
    parsed = parse_master_data(_complete(tmp_path))

    assert parsed.channels == ("海运", "空运")
    assert parsed.delivery_warehouses == ("义乌仓", "宁波仓")
    assert parsed.fba_warehouses == (
        ("AMAZON-ONT8", "2245 Cactus Ave, Ontario, CA 91761"),
        # A bare code is normalised, so it cannot land beside its prefixed twin.
        ("AMAZON-IAH3", "10384 W Airport Blvd, Houston, TX 77048"),
    )


def test_blank_rows_are_skipped(tmp_path: Path) -> None:
    source = build_master_data(
        tmp_path / "masters.xlsx",
        sheets={
            FBA_SHEET: [["FBA仓代码", "FBA仓详细地址"], ["", ""], ["ONT8", "Ontario"]],
            CHANNEL_SHEET: [["渠道"], [""], ["海运"], ["   "]],
            DELIVERY_SHEET: [["国内交货仓"], ["义乌仓"], [""]],
        },
    )

    parsed = parse_master_data(source)

    assert parsed.channels == ("海运",)
    assert parsed.delivery_warehouses == ("义乌仓",)
    assert parsed.fba_warehouses == (("AMAZON-ONT8", "Ontario"),)


def test_values_are_trimmed(tmp_path: Path) -> None:
    source = build_master_data(
        tmp_path / "masters.xlsx",
        sheets={
            FBA_SHEET: [["FBA仓代码", "FBA仓详细地址"], ["  ONT8 ", "  Ontario  "]],
            CHANNEL_SHEET: [["渠道"], ["  海运  "]],
            DELIVERY_SHEET: [["国内交货仓"], ["  义乌仓 "]],
        },
    )

    parsed = parse_master_data(source)

    assert parsed.fba_warehouses == (("AMAZON-ONT8", "Ontario"),)
    assert parsed.channels == ("海运",)
    assert parsed.delivery_warehouses == ("义乌仓",)


def test_a_missing_sheet_rejects_the_whole_file(tmp_path: Path) -> None:
    """Partial master data is worse than none: fail rather than half-update."""

    source = build_master_data(
        tmp_path / "masters.xlsx",
        sheets={
            FBA_SHEET: [["FBA仓代码", "FBA仓详细地址"]],
            CHANNEL_SHEET: [["渠道"]],
        },
    )

    with pytest.raises(WorkbookError) as caught:
        parse_master_data(source)

    assert caught.value.code == "MASTER_SHEET_MISSING"
    assert DELIVERY_SHEET in str(caught.value)


def test_an_unexpected_header_rejects_the_file(tmp_path: Path) -> None:
    source = build_master_data(
        tmp_path / "masters.xlsx",
        sheets={
            FBA_SHEET: [["仓代码", "地址"], ["ONT8", "Ontario"]],
            CHANNEL_SHEET: [["渠道"]],
            DELIVERY_SHEET: [["国内交货仓"]],
        },
    )

    with pytest.raises(WorkbookError) as caught:
        parse_master_data(source)

    assert caught.value.code == "MASTER_HEADER_MISMATCH"


def test_a_warehouse_without_an_address_is_rejected(tmp_path: Path) -> None:
    source = build_master_data(
        tmp_path / "masters.xlsx",
        sheets={
            FBA_SHEET: [["FBA仓代码", "FBA仓详细地址"], ["ONT8", ""]],
            CHANNEL_SHEET: [["渠道"]],
            DELIVERY_SHEET: [["国内交货仓"]],
        },
    )

    with pytest.raises(WorkbookError) as caught:
        parse_master_data(source)

    assert caught.value.code == "MASTER_ROW_INCOMPLETE"


def test_a_duplicate_code_in_the_file_is_rejected(tmp_path: Path) -> None:
    """Two rows for one code leave the intended address ambiguous."""

    source = build_master_data(
        tmp_path / "masters.xlsx",
        sheets={
            FBA_SHEET: [
                ["FBA仓代码", "FBA仓详细地址"],
                ["ONT8", "Ontario"],
                ["AMAZON-ONT8", "Somewhere else"],
            ],
            CHANNEL_SHEET: [["渠道"]],
            DELIVERY_SHEET: [["国内交货仓"]],
        },
    )

    with pytest.raises(WorkbookError) as caught:
        parse_master_data(source)

    assert caught.value.code == "MASTER_DUPLICATE_ROW"


def test_a_repeat_that_agrees_is_dropped(tmp_path: Path) -> None:
    """Real sheets repeat a code; an identical repeat is not ambiguous."""

    source = build_master_data(
        tmp_path / "masters.xlsx",
        sheets={
            FBA_SHEET: [
                ["FBA仓代码", "FBA仓详细地址"],
                ["ONT8", "Ontario"],
                ["AMAZON-ONT8", "Ontario"],
            ],
            CHANNEL_SHEET: [["渠道"]],
            DELIVERY_SHEET: [["国内交货仓"]],
        },
    )

    assert parse_master_data(source).fba_warehouses == (("AMAZON-ONT8", "Ontario"),)


def test_a_conflicting_repeat_names_both_rows(tmp_path: Path) -> None:
    source = build_master_data(
        tmp_path / "masters.xlsx",
        sheets={
            FBA_SHEET: [
                ["FBA仓代码", "FBA仓详细地址"],
                ["ONT8", "Ontario"],
                ["AMAZON-ONT8", "Somewhere else"],
            ],
            CHANNEL_SHEET: [["渠道"]],
            DELIVERY_SHEET: [["国内交货仓"]],
        },
    )

    with pytest.raises(WorkbookError) as caught:
        parse_master_data(source)

    message = str(caught.value)
    assert "第 2 行" in message and "第 3 行" in message
    assert "Ontario" in message and "Somewhere else" in message


def test_a_repeated_channel_name_is_dropped(tmp_path: Path) -> None:
    """A name-only row cannot disagree with itself, so a repeat is not an error."""

    source = build_master_data(
        tmp_path / "masters.xlsx",
        sheets={
            FBA_SHEET: [["FBA仓代码", "FBA仓详细地址"]],
            CHANNEL_SHEET: [["渠道"], ["海运"], ["海运"], ["空运"]],
            DELIVERY_SHEET: [["国内交货仓"], ["义乌仓"], ["义乌仓"]],
        },
    )

    parsed = parse_master_data(source)

    assert parsed.channels == ("海运", "空运")
    assert parsed.delivery_warehouses == ("义乌仓",)
