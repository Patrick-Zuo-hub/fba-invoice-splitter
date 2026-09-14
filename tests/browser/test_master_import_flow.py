from __future__ import annotations

from playwright.sync_api import Locator, Page, expect

from tests.browser.conftest import BrowserApp
from tests.fixtures.master_data_builder import (
    CHANNEL_SHEET,
    DELIVERY_SHEET,
    FBA_SHEET,
    build_master_data,
)


def _assert_reaches_the_pointer(locator: Locator) -> None:
    """Fail if something else sits on top of this control.

    Playwright's own click passed while the hidden file input covered the
    import button — it set the file programmatically and the click still
    landed — so occlusion has to be asserted directly.
    """

    locator.scroll_into_view_if_needed()
    covered = locator.evaluate(
        "el => {"
        "  const b = el.getBoundingClientRect();"
        "  const x = b.x + b.width / 2, y = b.y + b.height / 2;"
        "  if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return 'viewport';"
        "  const hit = document.elementFromPoint(x, y);"
        "  return hit === el || el.contains(hit) ? null :"
        "    (hit ? hit.tagName + (hit.id ? '#' + hit.id : '') : 'nothing');"
        "}"
    )
    assert covered is None, f"控件被 {covered} 遮挡"


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

    _assert_reaches_the_pointer(drawer.get_by_role("button", name="导入"))
    # The picker only opens if a click on the label reaches the label.
    _assert_reaches_the_pointer(drawer.locator(".file-picker"))

    page.locator("#masters-file").set_input_files(source)
    expect(drawer.locator("[data-masters-file-label]")).to_have_text("已选择：masters.xlsx")

    drawer.get_by_role("button", name="导入").click()

    summary = drawer.locator(".import-summary")
    expect(summary).to_contain_text("FBA 仓库：新增 1，更新 0")
    expect(summary).to_contain_text("运输渠道：新增 1，更新 0")
    expect(summary).to_contain_text("送货仓库：新增 0，更新 0")
    expect(drawer).to_contain_text("AMAZON-PHX3")
