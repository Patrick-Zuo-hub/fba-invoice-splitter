from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.browser.conftest import BrowserApp, apply_mapping, upload


def _open(page: Page, browser_app: BrowserApp) -> Page:
    upload(page, browser_app.source)
    apply_mapping(page, "ONT8\tS1\nIAH3\tS2")
    return page


def _options(page: Page) -> list[str]:
    return page.locator(
        '[data-shipment-row="S1"] [data-combo-list] li:visible'
    ).all_text_contents()


def test_empty_input_offers_every_channel(page: Page, browser_app: BrowserApp) -> None:
    _open(page, browser_app)
    combo = page.locator('[data-shipment-row="S1"] [data-combo-input]')

    combo.click()

    assert _options(page) == ["美森正班", "空运", "MATSON Express"]


def test_typing_narrows_to_substring_matches(page: Page, browser_app: BrowserApp) -> None:
    """Contains, not starts-with: the useful part of a channel name is rarely
    at the front."""

    _open(page, browser_app)
    combo = page.locator('[data-shipment-row="S1"] [data-combo-input]')

    combo.click()
    combo.fill("正班")

    assert _options(page) == ["美森正班"]


def test_filtering_ignores_case(page: Page, browser_app: BrowserApp) -> None:
    _open(page, browser_app)
    combo = page.locator('[data-shipment-row="S1"] [data-combo-input]')

    combo.click()
    combo.fill("mATSON")

    assert _options(page) == ["MATSON Express"]


def test_clearing_the_box_restores_every_option(page: Page, browser_app: BrowserApp) -> None:
    _open(page, browser_app)
    combo = page.locator('[data-shipment-row="S1"] [data-combo-input]')

    combo.click()
    combo.fill("正班")
    combo.fill("")

    assert _options(page) == ["美森正班", "空运", "MATSON Express"]


def test_choosing_an_option_saves_it(page: Page, browser_app: BrowserApp) -> None:
    _open(page, browser_app)
    row = page.locator('[data-shipment-row="S1"]')
    combo = row.locator("[data-combo-input]")

    combo.click()
    combo.fill("空")
    row.locator("[data-combo-list] li", has_text="空运").click()

    expect(combo).to_have_value("空运")
    expect(page.locator("#save-status")).to_have_text("已保存")
    # The saved value survives a re-render of the workspace.
    page.reload()


def test_a_value_that_matches_nothing_reverts_on_blur(
    page: Page, browser_app: BrowserApp
) -> None:
    """Otherwise a half-typed word would be sent and rejected by the server."""

    _open(page, browser_app)
    row = page.locator('[data-shipment-row="S1"]')
    combo = row.locator("[data-combo-input]")
    combo.click()
    row.locator("[data-combo-list] li", has_text="空运").click()
    expect(combo).to_have_value("空运")

    combo.fill("这不是一个渠道")
    page.locator("h1").click()

    expect(combo).to_have_value("空运")
