from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.browser.conftest import BrowserApp
from tests.fixtures.master_data_builder import (
    CHANNEL_SHEET,
    DELIVERY_SHEET,
    FBA_SHEET,
    build_master_data,
)


def test_master_data_import_reports_counts_in_the_drawer(
    page: Page, browser_app: BrowserApp
) -> None:
    source = build_master_data(
        browser_app.source.parent / "masters.xlsx",
        sheets={
            FBA_SHEET: [
                ["FBA仓代码", "FBA仓详细地址"],
                # ONT8 already exists with this address, so it is unchanged.
                ["ONT8", "Ontario, CA 91761"],
                ["PHX3", "6835 W Buckeye Rd, Phoenix, AZ 85043"],
            ],
            CHANNEL_SHEET: [["渠道"], ["海运"], ["空运"]],
            DELIVERY_SHEET: [["国内交货仓"], ["义乌仓"]],
        },
    )

    page.get_by_role("button", name="主数据").click()
    drawer = page.get_by_role("complementary").filter(has_text="主数据")
    expect(drawer.locator("[data-masters-file-label]")).to_have_text("选择主数据表格")

    page.locator("#masters-file").set_input_files(source)
    expect(drawer.locator("[data-masters-file-label]")).to_have_text("已选择：masters.xlsx")

    drawer.get_by_role("button", name="导入").click()

    summary = drawer.locator(".import-summary")
    expect(summary).to_contain_text("FBA 仓库：新增 1，更新 0")
    expect(summary).to_contain_text("运输渠道：新增 1，更新 0")
    expect(summary).to_contain_text("送货仓库：新增 0，更新 0")
    expect(drawer).to_contain_text("AMAZON-PHX3")
