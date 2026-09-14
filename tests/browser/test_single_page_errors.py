import re

from playwright.sync_api import Page, expect

from tests.browser.conftest import BrowserApp, apply_mapping, choose_channel, upload


def test_error_recovery_preserves_valid_row_values(
    page: Page, browser_app: BrowserApp
) -> None:
    upload(page, browser_app.broken)
    expect(page.locator("#upload-status")).not_to_be_empty()
    expect(page.locator("#upload")).not_to_have_class("is-collapsed")

    upload(page, browser_app.source)
    expect(page.get_by_text("已识别 2 个货件，解析完成")).to_be_visible()
    apply_mapping(page, "ONT8\tS1")
    expect(page.locator(".mapping-report")).to_contain_text("尚未映射：S2")
    expect(page.locator('[data-shipment-row="S1"] [data-field="warehouse_code"]')).to_have_value(
        "AMAZON-ONT8"
    )

    apply_mapping(page, "ONT8\tS1\nNEW1\tS2")
    s1 = page.locator('[data-shipment-row="S1"]')
    s2 = page.locator('[data-shipment-row="S2"]')
    expect(s1.locator('[data-field="warehouse_code"]')).to_have_value("AMAZON-ONT8")
    expect(s2.locator("[data-inline-address]")).to_be_visible()
    s2.locator("[data-inline-address]").fill("New warehouse test address")
    s2.locator("[data-save-address]").click()
    expect(s2.locator(".address-cell")).to_contain_text("New warehouse test address")

    choose_channel(s1, "空运")
    expect(s1.locator('[data-field="channel"]')).to_have_value("空运")
    result = page.evaluate(
        """async () => {
          const editor = document.querySelector('#shipment-editor');
          const response = await fetch(`/workspace/${editor.dataset.batchId}/shipments/S2`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json', 'X-FIS-Request': '1'},
            body: JSON.stringify({field: 'channel', value: '不存在渠道'})
          });
          return {status: response.status, body: await response.text()};
        }"""
    )
    assert result["status"] == 422
    expect(s1.locator('[data-field="channel"]')).to_have_value("空运")
    expect(s1.locator('[data-field="warehouse_code"]')).to_have_value("AMAZON-ONT8")


def test_upload_card_reflects_selection_success_and_failure(
    page: Page, browser_app: BrowserApp
) -> None:
    picker = page.locator(".file-picker")
    label = page.locator("[data-file-label]")
    status = page.locator("#upload-status")

    expect(label).to_have_text("选择 Excel 文件")
    expect(picker).to_have_css("cursor", "pointer")

    page.locator("#invoice").set_input_files(browser_app.source)
    expect(label).to_have_text(f"已选择：{browser_app.source.name}")

    page.get_by_role("button", name="上传并解析").click()
    expect(page.locator("#upload")).to_have_class(re.compile(r"\bis-collapsed\b"))
    file_bar = page.locator(".file-bar")
    expect(file_bar).to_contain_text(f"已上传：{browser_app.source.name}")
    expect(file_bar).to_contain_text("已识别 2 个货件")
    expect(file_bar.locator(".success-dot")).to_be_visible()
    expect(file_bar.get_by_role("button", name="重新上传")).to_be_visible()

    file_bar.get_by_role("button", name="重新上传").click()
    expect(page.locator("#upload")).not_to_have_class(re.compile(r"\bis-collapsed\b"))
    expect(label).to_have_text("选择 Excel 文件")

    page.locator("#invoice").set_input_files(browser_app.broken)
    page.get_by_role("button", name="上传并解析").click()
    expect(status).not_to_be_empty()
    expect(status).to_have_class(re.compile(r"\bis-error\b"))
    expect(status).to_have_css("color", "rgb(177, 38, 46)")
