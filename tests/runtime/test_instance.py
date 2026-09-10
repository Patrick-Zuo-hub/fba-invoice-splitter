from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Self

import pytest

from fis.runtime.instance import (
    ExistingInstance,
    InstanceRecord,
    InvalidInstanceRecord,
    acquire_instance,
    probe_fis,
    read_instance_record,
    write_instance_record,
)


def _record(*, pid: int = 123, port: int = 8768) -> InstanceRecord:
    return InstanceRecord(pid=pid, port=port, started_at="2026-09-10T09:00:00+00:00")


def test_record_round_trip_is_private_and_exact(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    write_instance_record(runtime, _record())

    assert read_instance_record(runtime) == _record()
    assert runtime.stat().st_mode & 0o077 == 0
    assert (runtime / "instance.json").stat().st_mode & 0o077 == 0
    assert set(json.loads((runtime / "instance.json").read_text()).keys()) == {
        "pid",
        "port",
        "started_at",
    }


@pytest.mark.parametrize(
    "payload",
    [
        '{"pid": 1, "pid": 2, "port": 8765, "started_at": "2026-09-10T09:00:00Z"}',
        '{"pid": true, "port": 8765, "started_at": "2026-09-10T09:00:00Z"}',
        '{"pid": 1, "port": 9000, "started_at": "2026-09-10T09:00:00Z"}',
        '{"pid": 1, "port": 8765, "started_at": "2026-09-10T09:00:00"}',
        '{"pid": 1, "port": 8765, "started_at": "2026-09-10T09:00:00Z", "x": []}',
        "[]",
        "{",
    ],
)
def test_record_rejects_malformed_or_unsafe_json(tmp_path: Path, payload: str) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "instance.json").write_text(payload)

    with pytest.raises(InvalidInstanceRecord):
        read_instance_record(runtime)


def test_record_rejects_oversize_and_symlink(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    record_path = runtime / "instance.json"
    record_path.write_bytes(b"x" * 4097)
    with pytest.raises(InvalidInstanceRecord):
        read_instance_record(runtime)

    record_path.unlink()
    target = tmp_path / "target.json"
    target.write_text('{}')
    record_path.symlink_to(target)
    with pytest.raises(InvalidInstanceRecord):
        read_instance_record(runtime)


def test_health_probe_requires_explicit_fis_product_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = ["not-fis"]

    class Response:
        def __init__(self) -> None:
            self.status = 200
            self.headers = {"X-FIS-Product": marker[0]}

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"status":"ok"}'

    class Opener:
        def open(self, url: str, *, timeout: float) -> Response:
            assert url == "http://127.0.0.1:8768/health"
            assert timeout < 1
            return Response()

    monkeypatch.setattr("fis.runtime.instance.build_opener", lambda *_args: Opener())

    assert probe_fis(8768) is False
    marker[0] = "fba-invoice-splitter"
    assert probe_fis(8768) is True


def test_held_lock_with_healthy_fis_returns_existing_instance(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    owner = acquire_instance(runtime, probe=lambda port: False)
    owner.write_record(_record())
    try:
        result = acquire_instance(runtime, probe=lambda port: port == 8768)
        assert result == ExistingInstance(port=8768)
    finally:
        owner.close()


def test_held_lock_with_non_fis_service_is_not_treated_as_existing(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    owner = acquire_instance(runtime, probe=lambda port: False)
    owner.write_record(_record())
    try:
        with pytest.raises(RuntimeError, match="无法确认"):
            acquire_instance(runtime, probe=lambda port: False)
    finally:
        owner.close()


def test_stale_or_corrupt_record_is_removed_only_after_lock_acquired(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    record_path = runtime / "instance.json"
    record_path.write_text("{")

    lease = acquire_instance(runtime, probe=lambda port: False)
    try:
        assert not record_path.exists()
    finally:
        lease.close()


def test_pid_reuse_does_not_make_an_unhealthy_unlocked_record_live(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    write_instance_record(runtime, _record(pid=os.getpid()))

    lease = acquire_instance(runtime, probe=lambda port: False)
    try:
        assert not (runtime / "instance.json").exists()
    finally:
        lease.close()


def test_cleanup_only_removes_record_owned_by_lease(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    lease = acquire_instance(runtime, probe=lambda port: False)
    ours = _record(pid=os.getpid())
    lease.write_record(ours)
    write_instance_record(runtime, _record(pid=os.getpid() + 1))

    lease.close()

    assert read_instance_record(runtime) == _record(pid=os.getpid() + 1)
