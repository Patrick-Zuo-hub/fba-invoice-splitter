from urllib.parse import urlsplit

from playwright.sync_api import Page, expect

from fis.xlsx.combined import combined_sheet_names
from tests.browser.conftest import BrowserApp, apply_mapping, upload


def test_primary_single_page_flow_uses_loopback_only(
    page: Page, browser_app: BrowserApp
) -> None:
    requested: list[str] = []
    page.on("request", lambda request: requested.append(request.url))

    upload(page, browser_app.source)
    expect(page.get_by_text("已识别 2 个货件，解析完成")).to_be_visible()
    apply_mapping(page, "ONT8\tS1\nIAH3\tS2")
    s1 = page.locator('[data-shipment-row="S1"]')
    s2 = page.locator('[data-shipment-row="S2"]')
    expect(s1.locator('[data-field="warehouse_code"]')).to_have_value("AMAZON-ONT8")
    expect(s1.locator(".address-cell")).to_contain_text("Ontario, CA 91761")

    s2.locator("[data-inline-address]").fill("Houston, TX 77032")
    s2.locator("[data-save-address]").click()
    expect(s2.locator(".address-cell")).to_contain_text("Houston, TX 77032")

    s1.locator('[data-field="channel"]').select_option(label="空运")
    expect(s1.locator('[data-field="channel"]')).to_have_value("空运")
    page.locator('[data-bulk-field="delivery_warehouse"]').click()
    page.locator("#bulk-value").select_option(label="义乌仓")
    page.locator("[data-bulk-confirm]").click()
    expect(s1.locator('[data-field="delivery_warehouse"]')).to_have_value("义乌仓")
    expect(s2.locator('[data-field="delivery_warehouse"]')).to_have_value("义乌仓")
    s2.locator('[data-field="channel"]').select_option(label="美森正班")
    expect(page.get_by_text("2 / 2 完整")).to_be_visible()

    page.get_by_role("button", name="选择输出文件夹").click()
    generate = page.get_by_role("button", name="生成工作簿")
    expect(generate).to_be_enabled()
    generate.click()
    expect(page.get_by_text("已生成 1 个工作簿，共包含 2 个货件 Sheet")).to_be_visible()
    expect(page.get_by_text("已处理_source.xlsx")).to_be_visible()
    assert sorted(path.name for path in browser_app.output.glob("*.xlsx")) == [
        "已处理_source.xlsx"
    ]
    assert combined_sheet_names(browser_app.output / "已处理_source.xlsx") == (
        "AMAZON-ONT8-S1",
        "AMAZON-IAH3-S2",
    )

    page.get_by_role("button", name="历史记录").click()
    expect(page.get_by_role("complementary").filter(has_text="历史记录")).to_contain_text(
        "source.xlsx"
    )
    page.get_by_role("button", name="关闭历史记录").click()
    page.get_by_role("button", name="主数据").click()
    expect(page.get_by_role("complementary").filter(has_text="主数据")).to_contain_text(
        "IAH3"
    )

    assert requested
    for url in requested:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"}:
            assert parsed.hostname == "127.0.0.1"
