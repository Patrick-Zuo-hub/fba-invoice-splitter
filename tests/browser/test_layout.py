from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.browser.conftest import BrowserApp

# A common laptop width, narrower than the 1120px the body insists on. This is
# where the vw-based width used to leave a wide gap down the left-hand side.
LAPTOP = {"width": 1280, "height": 800}
NARROW = {"width": 1100, "height": 800}


def _left_gap(page: Page, selector: str) -> float:
    box = page.locator(selector).bounding_box()
    assert box is not None
    return box["x"]


@pytest.mark.parametrize("viewport", [LAPTOP, NARROW], ids=["laptop", "narrow"])
def test_content_starts_near_the_left_edge(
    page: Page, browser_app: BrowserApp, viewport: dict[str, int]
) -> None:
    """Sizing the main column in vw while the body has a px min-width centred
    it inside a body wider than the viewport, so the work area sat off to the
    right and had to be scrolled to."""

    page.set_viewport_size(viewport)
    page.goto(browser_app.url)

    assert _left_gap(page, ".app-main") <= 32
    assert _left_gap(page, ".app-header") <= 32


def test_main_column_fills_the_available_width(page: Page, browser_app: BrowserApp) -> None:
    page.set_viewport_size(LAPTOP)
    page.goto(browser_app.url)

    box = page.locator(".app-main").bounding_box()
    body = page.evaluate("document.body.getBoundingClientRect().width")
    assert box is not None
    # Gutters only, not a centred column floating in a wider body.
    assert body - box["width"] <= 64


def test_a_disabled_generate_button_says_what_is_missing(
    page: Page, browser_app: BrowserApp
) -> None:
    """It was indistinguishable from a broken button: clicking did nothing and
    nothing said why."""

    from playwright.sync_api import expect

    from tests.browser.conftest import apply_mapping, upload

    upload(page, browser_app.source)
    apply_mapping(page, "ONT8\tS1\nIAH3\tS2")

    generate = page.locator("#generation-action .generate")
    expect(generate).to_be_disabled()
    hint = page.locator("#generation-hint")
    expect(hint).to_contain_text("尚未选择输出文件夹")
    assert "暂时无法生成" in (generate.get_attribute("title") or "")

    page.get_by_role("button", name="选择输出文件夹").click()
    expect(hint).to_contain_text("仍有货件未填写完整")
    expect(hint).not_to_contain_text("尚未选择输出文件夹")
