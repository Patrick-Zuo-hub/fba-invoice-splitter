import time

from playwright.sync_api import Page

from fis.runtime.lifecycle import Lifecycle
from tests.browser.conftest import BrowserApp


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_page_heartbeat_and_close_feed_shutdown_lifecycle(
    page: Page, browser_app: BrowserApp
) -> None:
    deadline = time.monotonic() + 2
    while browser_app.app.state.lifecycle.snapshot().live_pages != 1:
        if time.monotonic() >= deadline:
            raise AssertionError("initial page heartbeat was not registered")
        time.sleep(0.01)

    page.reload()
    deadline = time.monotonic() + 2
    while browser_app.app.state.lifecycle.snapshot().live_pages != 1:
        if time.monotonic() >= deadline:
            raise AssertionError("refresh did not replace the live page heartbeat")
        time.sleep(0.01)

    page.close()
    deadline = time.monotonic() + 2
    while browser_app.app.state.lifecycle.snapshot().live_pages != 0:
        if time.monotonic() >= deadline:
            raise AssertionError("page close did not remove the live page")
        time.sleep(0.01)


def test_thirty_second_grace_refresh_recovery_and_active_job_delay() -> None:
    clock = Clock()
    lifecycle = Lifecycle(clock=clock)
    page_id = lifecycle.register_page()
    lifecycle.remove_page(page_id)
    clock.advance(29.999)
    assert lifecycle.should_exit() is False

    refreshed = lifecycle.register_page()
    clock.advance(30)
    assert lifecycle.should_exit() is False
    lifecycle.remove_page(refreshed)
    lifecycle.begin_job()
    clock.advance(30)
    assert lifecycle.should_exit() is False
    lifecycle.end_job()
    assert lifecycle.should_exit() is True
