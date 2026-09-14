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


def test_every_offered_option_can_actually_be_clicked(
    page: Page, browser_app: BrowserApp
) -> None:
    """Positioned inside the table scroller the list was cut off part-way down,
    so options the filter kept were still out of reach."""

    _open(page, browser_app)
    row = page.locator('[data-shipment-row="S1"]')
    row.locator("[data-combo-input]").click()

    unreachable = page.evaluate(
        """() => {
          const items = Array.from(
            document.querySelectorAll('[data-shipment-row="S1"] [data-combo-list] li')
          ).filter((li) => !li.hidden);
          return items
            .filter((li) => {
              const b = li.getBoundingClientRect();
              const x = b.x + b.width / 2, y = b.y + b.height / 2;
              if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return true;
              const hit = document.elementFromPoint(x, y);
              return !(hit === li || li.contains(hit));
            })
            .map((li) => li.textContent.trim());
        }"""
    )

    assert unreachable == [], f"这些选项点不到：{unreachable}"
    assert row.locator("[data-combo-list] li:visible").count() == 3


def test_bulk_fill_offers_the_same_filtered_list(page: Page, browser_app: BrowserApp) -> None:
    """The batch control used to be cloned only from a <select>; once the field
    became a combobox it silently degraded to a plain text box."""

    _open(page, browser_app)
    page.locator('[data-bulk-field="channel"]').click()
    popover = page.locator("#bulk-popover")

    combo = popover.locator("[data-combo-input]")
    expect(combo).to_be_visible()
    combo.click()
    combo.fill("mATSON")
    assert popover.locator("[data-combo-list] li:visible").all_text_contents() == [
        "MATSON Express"
    ]

    popover.locator("[data-combo-list] li", has_text="MATSON Express").click()
    expect(combo).to_have_value("MATSON Express")

    popover.locator("[data-bulk-confirm]").click()
    for row in ("S1", "S2"):
        expect(
            page.locator(f'[data-shipment-row="{row}"] [data-combo-input]')
        ).to_have_value("MATSON Express")


def test_enter_does_not_choose_an_option(page: Page, browser_app: BrowserApp) -> None:
    """With a Chinese IME, Enter commits the candidate text — it is the IME's
    key, not ours. Claiming it selected the first row of the list instead."""

    _open(page, browser_app)
    row = page.locator('[data-shipment-row="S1"]')
    combo = row.locator("[data-combo-input]")

    combo.click()
    # "ma" matches MATSON Express, so Enter had something to wrongly select.
    combo.fill("ma")
    combo.press("Enter")

    # What the user typed stays put; nothing was chosen behind their back.
    expect(combo).to_have_value("ma")
    expect(row.locator("[data-combo-list]")).to_be_visible()


def test_escape_while_composing_leaves_the_text_alone(
    page: Page, browser_app: BrowserApp
) -> None:
    """The IME uses Escape to dismiss its candidates; resetting the field then
    would throw away what the user was in the middle of typing."""

    _open(page, browser_app)
    combo = page.locator('[data-shipment-row="S1"] [data-combo-input]')
    combo.click()
    combo.fill("ca")

    combo.evaluate(
        "el => el.dispatchEvent(new KeyboardEvent('keydown', "
        "{key: 'Escape', isComposing: true, bubbles: true}))"
    )

    expect(combo).to_have_value("ca")


def test_typing_a_full_name_still_saves_without_enter(
    page: Page, browser_app: BrowserApp
) -> None:
    """Enter is gone, so the exact-match-on-blur path has to carry the weight."""

    _open(page, browser_app)
    row = page.locator('[data-shipment-row="S1"]')
    combo = row.locator("[data-combo-input]")

    combo.click()
    combo.fill("MATSON Express")
    page.locator("h1").click()

    expect(combo).to_have_value("MATSON Express")
    expect(page.locator("#save-status")).to_have_text("已保存")
