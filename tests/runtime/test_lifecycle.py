from __future__ import annotations

import pytest

from fis.runtime.lifecycle import Lifecycle


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_startup_allows_first_page_window_and_exits_at_grace_boundary() -> None:
    clock = Clock()
    lifecycle = Lifecycle(clock=clock)

    clock.advance(29.999)
    assert lifecycle.should_exit() is False
    clock.advance(0.001)
    assert lifecycle.should_exit() is True
    assert lifecycle.shutdown_event.is_set()


def test_heartbeat_expires_after_fifteen_seconds_then_waits_grace() -> None:
    clock = Clock()
    lifecycle = Lifecycle(clock=clock)
    lifecycle.register_page()

    clock.advance(15)
    assert lifecycle.should_exit() is False
    clock.advance(29.999)
    assert lifecycle.should_exit() is False
    clock.advance(0.001)
    assert lifecycle.should_exit() is True
    assert lifecycle.snapshot().live_pages == 0


def test_refresh_or_reopen_cancels_pending_exit() -> None:
    clock = Clock()
    lifecycle = Lifecycle(clock=clock)
    old = lifecycle.register_page()
    lifecycle.remove_page(old)
    clock.advance(29)

    new = lifecycle.register_page()
    clock.advance(20)
    lifecycle.heartbeat(new)

    assert lifecycle.should_exit() is False


def test_multiple_pages_keep_process_alive_while_any_is_live() -> None:
    clock = Clock()
    lifecycle = Lifecycle(clock=clock)
    first = lifecycle.register_page()
    second = lifecycle.register_page()
    lifecycle.remove_page(first)
    clock.advance(14)
    lifecycle.heartbeat(second)
    clock.advance(20)

    assert lifecycle.should_exit() is False


def test_job_blocks_exit_and_completion_after_elapsed_grace_is_immediate() -> None:
    clock = Clock()
    lifecycle = Lifecycle(clock=clock)
    page = lifecycle.register_page()
    lifecycle.begin_job()
    lifecycle.remove_page(page)
    clock.advance(31)

    assert lifecycle.should_exit() is False
    lifecycle.end_job()
    assert lifecycle.should_exit() is True


def test_job_reference_count_cannot_be_negative() -> None:
    lifecycle = Lifecycle(clock=Clock())

    with pytest.raises(RuntimeError, match="zero"):
        lifecycle.end_job()
