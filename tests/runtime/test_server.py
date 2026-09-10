from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

import pytest

from fis.config import Settings
from fis.runtime.instance import InstanceRecord, read_instance_record
from fis.runtime.lifecycle import Lifecycle
from fis.runtime.server import (
    PORTS,
    StartupError,
    bind_first_loopback_port,
    launch,
)


class FakeSocket:
    def __init__(self, occupied: set[int]) -> None:
        self.occupied = occupied
        self.bound_address: tuple[str, int] | None = None
        self.listened = False
        self.closed = False
        self.options: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))

    def bind(self, address: tuple[str, int]) -> None:
        if address[1] in self.occupied:
            raise OSError("occupied")
        self.bound_address = address

    def listen(self) -> None:
        self.listened = True

    def close(self) -> None:
        self.closed = True


def test_port_selection_uses_first_bindable_port_and_closes_failures() -> None:
    made: list[FakeSocket] = []

    def factory() -> FakeSocket:
        candidate = FakeSocket({8765, 8766})
        made.append(candidate)
        return candidate

    binding = bind_first_loopback_port(factory)  # type: ignore[arg-type]

    assert binding.port == 8767
    assert binding.socket.bound_address == ("127.0.0.1", 8767)  # type: ignore[attr-defined]
    assert [candidate.closed for candidate in made] == [True, True, False]
    binding.socket.close()


def test_all_ports_occupied_raises_chinese_error_and_closes_every_socket() -> None:
    made: list[FakeSocket] = []

    def factory() -> FakeSocket:
        candidate = FakeSocket(set(PORTS))
        made.append(candidate)
        return candidate

    with pytest.raises(StartupError, match="8765.*8775"):
        bind_first_loopback_port(factory)  # type: ignore[arg-type]
    assert len(made) == len(PORTS)
    assert all(candidate.closed for candidate in made)


def test_real_socket_is_reserved_until_caller_closes_it() -> None:
    binding = bind_first_loopback_port(
        lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM), ports=(0,)
    )
    try:
        assert binding.socket.getsockname()[0] == "127.0.0.1"
        contender = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError):
                contender.bind(binding.socket.getsockname())
        finally:
            contender.close()
    finally:
        binding.socket.close()


class FakeServer:
    def __init__(self) -> None:
        self.started = False
        self.should_exit = False
        self.received: list[socket.socket] | None = None

    async def serve(self, *, sockets: list[socket.socket]) -> None:
        self.received = sockets
        self.started = True
        while not self.should_exit:
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_launcher_opens_browser_only_after_ready_and_cleans_up(tmp_path: Path) -> None:
    events: list[str] = []
    server = FakeServer()
    lifecycle = Lifecycle(clock=lambda: 100.0, grace_seconds=0)
    reserved = FakeSocket(set())

    def browser_open(url: str) -> bool:
        assert server.started
        assert read_instance_record(tmp_path / "data" / "runtime").port in PORTS
        events.append(url)
        return True

    result = await launch(
        Settings(data_root=tmp_path / "data"),
        browser_open=browser_open,
        lifecycle=lifecycle,
        socket_factory=lambda: reserved,
        server_factory=lambda app: server,
        poll_interval=0,
    )

    assert result.started is True
    assert events == [f"http://127.0.0.1:{result.port}/"]
    assert server.received is not None and len(server.received) == 1
    assert server.received[0] is reserved
    assert reserved.closed is True
    assert not (tmp_path / "data" / "runtime" / "instance.json").exists()


@pytest.mark.asyncio
async def test_browser_failure_stops_server_and_cleans_up(tmp_path: Path) -> None:
    server = FakeServer()
    reserved = FakeSocket(set())

    with pytest.raises(StartupError, match="浏览器"):
        await launch(
            Settings(data_root=tmp_path / "data"),
            browser_open=lambda url: False,
            lifecycle=Lifecycle(clock=lambda: 100.0),
            socket_factory=lambda: reserved,
            server_factory=lambda app: server,
            poll_interval=0,
        )

    assert server.should_exit is True
    assert not (tmp_path / "data" / "runtime" / "instance.json").exists()


@pytest.mark.asyncio
async def test_browser_exception_stops_server_and_cleans_up(tmp_path: Path) -> None:
    server = FakeServer()
    reserved = FakeSocket(set())

    def fail_to_open(_url: str) -> bool:
        raise OSError("desktop integration failed")

    with pytest.raises(StartupError, match="浏览器"):
        await launch(
            Settings(data_root=tmp_path / "data"),
            browser_open=fail_to_open,
            lifecycle=Lifecycle(clock=lambda: 100.0),
            socket_factory=lambda: reserved,
            server_factory=lambda app: server,
            poll_interval=0,
        )

    assert server.should_exit is True
    assert reserved.closed is True
    assert not (tmp_path / "data" / "runtime" / "instance.json").exists()


@pytest.mark.asyncio
async def test_existing_healthy_instance_only_opens_browser(tmp_path: Path) -> None:
    from fis.runtime.instance import acquire_instance

    runtime = tmp_path / "data" / "runtime"
    owner = acquire_instance(runtime, probe=lambda port: False)
    owner.write_record(
        InstanceRecord(pid=321, port=8769, started_at="2026-09-10T09:00:00+00:00")
    )
    opened: list[str] = []
    made_server = False

    def server_factory(app: Any) -> FakeServer:
        nonlocal made_server
        made_server = True
        return FakeServer()

    try:
        result = await launch(
            Settings(data_root=tmp_path / "data"),
            browser_open=lambda url: opened.append(url) is None,
            probe=lambda port: port == 8769,
            server_factory=server_factory,
        )
    finally:
        owner.close()

    assert result.started is False
    assert result.port == 8769
    assert opened == ["http://127.0.0.1:8769/"]
    assert made_server is False
