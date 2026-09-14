from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class LifecycleSnapshot:
    live_pages: int
    active_jobs: int
    no_page_since: float | None
    exit_eligible: bool


class Lifecycle:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        # How stale a page's last heartbeat may be before that page is treated
        # as gone — not how often the page beats, which is every five seconds.
        # Thirty minutes because browsers throttle a hidden tab's timers, often
        # to once a minute: at fifteen seconds FIS exited while the user was
        # merely looking at another window. Closing a page does not wait for
        # this; pagehide reports it directly and the grace period applies.
        heartbeat_seconds: float = 30 * 60,
        grace_seconds: float = 30,
    ) -> None:
        self._clock = clock
        self._heartbeat_seconds = heartbeat_seconds
        self._grace_seconds = grace_seconds
        self._heartbeats: dict[str, float] = {}
        self._active_jobs = 0
        self._no_page_since: float | None = clock()
        self._lock = threading.RLock()
        self.shutdown_event = threading.Event()

    def register_page(self) -> str:
        page_id = secrets.token_urlsafe(18)
        self.heartbeat(page_id)
        return page_id

    def heartbeat(self, page_id: str) -> None:
        with self._lock:
            self._heartbeats[page_id] = self._clock()
            self._no_page_since = None

    def remove_page(self, page_id: str) -> None:
        with self._lock:
            self._heartbeats.pop(page_id, None)
            if not self._heartbeats and self._no_page_since is None:
                self._no_page_since = self._clock()

    def begin_job(self) -> None:
        with self._lock:
            self._active_jobs += 1

    def end_job(self) -> None:
        with self._lock:
            if self._active_jobs == 0:
                raise RuntimeError("active job count is already zero")
            self._active_jobs -= 1

    def _snapshot_locked(self) -> LifecycleSnapshot:
        now = self._clock()
        expired = [
            page_id
            for page_id, heartbeat in self._heartbeats.items()
            if now - heartbeat > self._heartbeat_seconds
        ]
        expired_heartbeats = [self._heartbeats.pop(page_id) for page_id in expired]
        if self._heartbeats:
            self._no_page_since = None
        elif self._no_page_since is None:
            self._no_page_since = (
                max(expired_heartbeats) + self._heartbeat_seconds
                if expired_heartbeats
                else now
            )
        eligible = (
            self._active_jobs == 0
            and self._no_page_since is not None
            and now - self._no_page_since >= self._grace_seconds
        )
        return LifecycleSnapshot(
            live_pages=len(self._heartbeats),
            active_jobs=self._active_jobs,
            no_page_since=self._no_page_since,
            exit_eligible=eligible,
        )

    def snapshot(self) -> LifecycleSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def should_exit(self) -> bool:
        with self._lock:
            eligible = self._snapshot_locked().exit_eligible
            if eligible:
                self.shutdown_event.set()
            return eligible
