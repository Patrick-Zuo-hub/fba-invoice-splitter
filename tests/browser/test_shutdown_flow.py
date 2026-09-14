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


def test_a_second_tab_keeps_the_server_alive(page: Page, browser_app: BrowserApp) -> None:
    """Closing one window only stops FIS if it was the last one: each tab
    heartbeats under its own id."""

    lifecycle = browser_app.app.state.lifecycle
    second = page.context.new_page()
    second.goto(browser_app.url)

    deadline = time.monotonic() + 2
    while lifecycle.snapshot().live_pages != 2:
        if time.monotonic() >= deadline:
            raise AssertionError("the second tab did not register")
        time.sleep(0.01)

    page.close()
    deadline = time.monotonic() + 2
    while lifecycle.snapshot().live_pages != 1:
        if time.monotonic() >= deadline:
            raise AssertionError("closing one tab should leave the other")
        time.sleep(0.01)
    assert lifecycle.should_exit() is False

    second.close()
    deadline = time.monotonic() + 2
    while lifecycle.snapshot().live_pages != 0:
        if time.monotonic() >= deadline:
            raise AssertionError("closing the last tab did not remove it")
        time.sleep(0.01)


def test_closing_mid_generation_waits_for_the_job(page: Page, browser_app: BrowserApp) -> None:
    """A close during generation must not cut the workbook short."""

    lifecycle = browser_app.app.state.lifecycle
    deadline = time.monotonic() + 2
    while lifecycle.snapshot().live_pages != 1:
        if time.monotonic() >= deadline:
            raise AssertionError("page never registered")
        time.sleep(0.01)

    lifecycle.begin_job()
    page.close()
    deadline = time.monotonic() + 2
    while lifecycle.snapshot().live_pages != 0:
        if time.monotonic() >= deadline:
            raise AssertionError("page close did not remove the live page")
        time.sleep(0.01)

    snapshot = lifecycle.snapshot()
    assert snapshot.active_jobs == 1
    assert snapshot.exit_eligible is False, "生成进行中不应退出"

    lifecycle.end_job()
