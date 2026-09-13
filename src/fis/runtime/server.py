from __future__ import annotations

import asyncio
import os
import socket
import sys
import webbrowser
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

import uvicorn
from fastapi import FastAPI

from fis.config import Settings
from fis.runtime.instance import (
    ExistingInstance,
    HealthProbe,
    InstanceRecord,
    StartupError,
    acquire_instance,
    probe_fis,
    runtime_identity,
)
from fis.runtime.lifecycle import Lifecycle
from fis.web.app import create_app

PORTS = tuple(range(8765, 8776))


class SocketLike(Protocol):
    def setsockopt(self, level: int, option: int, value: int) -> None: ...
    def bind(self, address: tuple[str, int]) -> None: ...
    def listen(self) -> None: ...
    def close(self) -> None: ...


class ServerLike(Protocol):
    started: bool
    should_exit: bool

    async def serve(self, *, sockets: list[socket.socket]) -> None: ...


@dataclass(frozen=True)
class BoundSocket:
    socket: socket.socket
    port: int


@dataclass(frozen=True)
class LaunchResult:
    port: int
    started: bool


def _new_socket() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def bind_first_loopback_port(
    factory: Callable[[], SocketLike] = _new_socket,
    ports: Iterable[int] = PORTS,
) -> BoundSocket:
    attempted: list[int] = []
    for port in ports:
        candidate = factory()
        try:
            if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            candidate.bind(("127.0.0.1", port))
            candidate.listen()
            return BoundSocket(cast(socket.socket, candidate), port)
        except OSError:
            candidate.close()
            attempted.append(port)
    if attempted:
        span = f"{min(attempted)}–{max(attempted)}"
    else:
        span = "8765–8775"
    raise StartupError(f"端口 {span} 均被占用：{attempted}")


def _default_server_factory(app: FastAPI) -> ServerLike:
    config = uvicorn.Config(app, host="127.0.0.1", log_level="info", lifespan="on")
    return uvicorn.Server(config)


async def _wait_until_ready(server: ServerLike, task: asyncio.Task[None], delay: float) -> None:
    while not server.started:
        if task.done():
            await task
            raise StartupError("本地服务未能启动")
        await asyncio.sleep(delay)


async def launch(
    settings: Settings,
    *,
    browser_open: Callable[[str], bool] = webbrowser.open,
    probe: HealthProbe = probe_fis,
    lifecycle: Lifecycle | None = None,
    socket_factory: Callable[[], SocketLike] = _new_socket,
    server_factory: Callable[[FastAPI], ServerLike] = _default_server_factory,
    poll_interval: float = 0.05,
) -> LaunchResult:
    runtime_dir = settings.data_root / "runtime"
    # Only adopt an instance that serves this data root: a stale record's
    # port may since have been taken by a FIS started elsewhere.
    identity = runtime_identity(runtime_dir)
    ownership = acquire_instance(
        runtime_dir, probe=lambda port: probe(port, identity=identity)
    )
    if isinstance(ownership, ExistingInstance):
        url = f"http://127.0.0.1:{ownership.port}/"
        try:
            opened = browser_open(url)
        except Exception as exc:
            raise StartupError("无法打开浏览器，请手动打开 FIS 地址") from exc
        if not opened:
            raise StartupError("无法打开浏览器，请手动打开 FIS 地址")
        return LaunchResult(port=ownership.port, started=False)

    lease = ownership
    binding: BoundSocket | None = None
    server: ServerLike | None = None
    server_task: asyncio.Task[None] | None = None
    try:
        binding = bind_first_loopback_port(socket_factory)
        state = lifecycle or Lifecycle()
        app = create_app(settings, bound_port=binding.port)
        app.state.lifecycle = state
        record = InstanceRecord(
            pid=os.getpid(),
            port=binding.port,
            started_at=datetime.now(UTC).isoformat(),
        )
        lease.write_record(record)
        server = server_factory(app)
        server_task = asyncio.create_task(server.serve(sockets=[binding.socket]))
        await _wait_until_ready(server, server_task, poll_interval)
        url = f"http://127.0.0.1:{binding.port}/"
        try:
            opened = browser_open(url)
        except Exception as exc:
            raise StartupError("无法打开浏览器，本地服务已停止；请重新启动 FIS") from exc
        if not opened:
            raise StartupError("无法打开浏览器，本地服务已停止；请重新启动 FIS")
        while not state.should_exit():
            if server_task.done():
                await server_task
                raise StartupError("本地服务意外停止")
            await asyncio.sleep(poll_interval)
        server.should_exit = True
        await server_task
        return LaunchResult(port=binding.port, started=True)
    finally:
        if server is not None:
            server.should_exit = True
        if server_task is not None and not server_task.done():
            await server_task
        if binding is not None:
            binding.socket.close()
        lease.close()


__all__ = [
    "PORTS",
    "BoundSocket",
    "LaunchResult",
    "StartupError",
    "bind_first_loopback_port",
    "launch",
]
