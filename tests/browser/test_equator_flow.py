from playwright.sync_api import Page, expect

from tests.browser.conftest import BrowserApp, apply_mapping, upload
from tests.fixtures.equator_builder import IDS, build_equator


def test_equator_mapping_channel_code_bulk_override_and_single_sheet(
    page: Page, browser_app: BrowserApp,
) -> None:
    source = browser_app.source.with_name("equator.xlsx")
    build_equator(source, external=True)
    upload(page, source)
    expect(page.locator("[data-equator]")).to_be_visible()
    expect(page.get_by_label("客户编码", exact=True)).to_have_value("深圳博致咨询有限公司")
    apply_mapping(page, f"SMF3\t{IDS[0]}\nTEB9\t{IDS[1]}")
    rows = page.locator("[data-shipment-row]")
    expect(rows).to_have_count(2)
    expect(rows.first.locator("[data-value-field=warehouse_code]")).to_have_text("SMF3")
    expect(rows.first.locator(".address-cell")).to_contain_text("3923 S B ST")
    page.locator('[data-bulk-field="channel"]').click()
    page.locator("#bulk-value").fill("美森 包税")
    page.locator('#bulk-popover [data-combo-value="美东美森卡派(包税)"]').click()
    page.locator("[data-bulk-confirm]").click()
    expect(rows.first.locator("[data-channel-code]")).to_have_text("CD0016")
    expect(rows.nth(1).locator("[data-channel-code]")).to_have_text("CD0016")
    second = rows.nth(1)
    second.locator("[data-combo-input]").fill("CD0209")
    second.locator('[data-combo-value="美西美森大货中转(不包税)"]').click()
    expect(second.locator("[data-channel-code]")).to_have_text("CD0209")
    expect(rows.first.locator("[data-channel-code]")).to_have_text("CD0016")
    page.locator('[data-bulk-field="delivery_warehouse"]').click()
    page.locator("#bulk-value").select_option("赤道深圳仓")
    page.locator("[data-bulk-confirm]").click()
    expect(page.locator("#completion-badge")).to_have_text("2 / 2 完整")
    page.locator(".equator-preview summary").click()
    page.locator("[data-equator-preview]").click()
    preview = page.locator("#equator-preview-content tbody tr")
    expect(preview).to_have_count(4)
    expect(preview.nth(2)).to_contain_text("CD0209")
    expect(preview.nth(0).locator("td").nth(3)).to_have_text(IDS[0])
    expect(preview.nth(1).locator("td").nth(3)).to_have_text(IDS[0])
    expect(preview.nth(2).locator("td").nth(3)).to_have_text(IDS[1])
    expect(preview.nth(3).locator("td").nth(3)).to_have_text(IDS[1])
    page.get_by_role("button", name="选择输出文件夹").click()
    page.get_by_role("button", name="生成单 Sheet 工作簿").click()
    expect(page.locator("#generation-result")).to_contain_text("全部 4 条明细")
    assert (browser_app.output / "已处理_equator.xlsx").is_file()
