from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Self

import pytest

import fis.runtime.instance as instance_module
from fis.runtime.instance import (
    ExistingInstance,
    InstanceRecord,
    InvalidInstanceRecord,
    StartupError,
    acquire_instance,
    probe_fis,
    read_instance_record,
    write_instance_record,
)


def _assert_owner_only(path: Path) -> None:
    """Assert only the owner can reach ``path``, however the platform says it.

    POSIX mode bits are meaningless on Windows — os.stat reports 0o777 for
    every directory — so check the ACL the runtime actually enforces there.
    """

    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        return
    api = instance_module._CtypesWindowsApi()
    if path.is_dir():
        handle, info = api.open_directory(path)
    else:
        handle, info = api.open_file(path, access="read", creation="open_existing")
    try:
        assert info.acl_safe, info.acl_detail
    finally:
        api.close(handle)


def _plant_record(runtime: Path, payload: bytes) -> Path:
    """Leave a record behind the way a previous FIS run would have.

    Writing it with plain Python gives the file whatever ACL it inherits, and
    the runtime correctly refuses a record it cannot prove is private — so the
    fixture has to create it through the same API production uses.
    """

    record = runtime / "instance.json"
    if os.name != "nt":
        record.write_bytes(payload)
        record.chmod(0o600)
        return record
    api = instance_module._CtypesWindowsApi()
    handle, _ = api.open_file(record, access="write", creation="create_new")
    try:
        api.write(handle, payload)
        api.flush(handle)
    finally:
        api.close(handle)
    return record


def _make_runtime_dir(path: Path) -> Path:
    """Create a runtime directory the way FIS itself would, per platform.

    A bare ``mkdir`` is POSIX thinking: on Windows it inherits the parent's
    ACL, which the runtime directory check then correctly rejects. Production
    never hits this because FIS creates the directory itself.
    """

    if os.name == "nt":
        instance_module._CtypesWindowsApi().create_directory(path)
    else:
        path.mkdir(mode=0o700)
    return path


def _record(*, pid: int = 123, port: int = 8768) -> InstanceRecord:
    return InstanceRecord(pid=pid, port=port, started_at="2026-09-10T09:00:00+00:00")


def test_record_round_trip_is_private_and_exact(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    write_instance_record(runtime, _record())

    assert read_instance_record(runtime) == _record()
    _assert_owner_only(runtime)
    _assert_owner_only(runtime / "instance.json")
    assert set(json.loads((runtime / "instance.json").read_text()).keys()) == {
        "pid",
        "port",
        "started_at",
    }


def test_reading_missing_record_does_not_create_runtime_directory(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"

    with pytest.raises(FileNotFoundError):
        read_instance_record(runtime)

    assert not runtime.exists()


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
    runtime = _make_runtime_dir(tmp_path / "runtime")
    (runtime / "instance.json").write_text(payload)

    with pytest.raises(InvalidInstanceRecord):
        read_instance_record(runtime)


def test_record_rejects_oversize_and_symlink(tmp_path: Path) -> None:
    runtime = _make_runtime_dir(tmp_path / "runtime")
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
        _assert_owner_only(runtime / "instance.lock")
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
    runtime = _make_runtime_dir(tmp_path / "runtime")
    record_path = _plant_record(runtime, b"{")

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


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd race injection")
def test_directory_swap_during_publish_cannot_write_outside(tmp_path: Path) -> None:
    runtime = tmp_path / "data" / "runtime"
    outside = tmp_path / "outside"
    outside.mkdir()
    retained = tmp_path / "retained-runtime"

    def swap_directory(stage: str) -> None:
        if stage != "before_record_publish":
            return
        runtime.rename(retained)
        runtime.symlink_to(outside, target_is_directory=True)
        for temporary in retained.glob(".instance-*"):
            (outside / temporary.name).write_text("outside sentinel")

    with pytest.raises(StartupError, match="运行目录"):
        write_instance_record(runtime, _record(), _operation_hook=swap_directory)

    assert not (outside / "instance.json").exists()
    assert all(path.read_text() == "outside sentinel" for path in outside.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission race injection")
def test_directory_permission_change_during_publish_is_fail_closed(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"

    def broaden_directory(stage: str) -> None:
        if stage == "before_record_publish":
            runtime.chmod(0o755)

    with pytest.raises(StartupError, match="权限"):
        write_instance_record(runtime, _record(), _operation_hook=broaden_directory)

    assert not (runtime / "instance.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX openat race injection")
def test_record_swap_before_open_never_reads_symlink_target(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    write_instance_record(runtime, _record())
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({**json.loads((runtime / "instance.json").read_text()), "pid": 999}))

    def swap_record(stage: str) -> None:
        if stage == "before_record_open":
            (runtime / "instance.json").unlink()
            (runtime / "instance.json").symlink_to(outside)

    with pytest.raises(InvalidInstanceRecord):
        read_instance_record(runtime, _operation_hook=swap_record)

    assert json.loads(outside.read_text())["pid"] == 999


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link semantics")
def test_record_hardlink_is_rejected_without_touching_external_link(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"pid": 123, "port": 8768, "started_at": "2026-09-10T09:00:00Z"}))
    os.chmod(outside, 0o600)
    os.link(outside, runtime / "instance.json")

    with pytest.raises(InvalidInstanceRecord, match="hard link"):
        read_instance_record(runtime)

    assert outside.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX openat cleanup semantics")
def test_acquire_rejects_symlink_record_without_deleting_target(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    outside = tmp_path / "outside.json"
    outside.write_text("outside sentinel")
    (runtime / "instance.json").symlink_to(outside)

    with pytest.raises(StartupError, match="旧实例记录不可信"):
        acquire_instance(runtime, probe=lambda port: False)

    assert outside.read_text() == "outside sentinel"
    assert (runtime / "instance.json").is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link semantics")
def test_lock_hardlink_is_rejected_without_locking_target(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    outside = tmp_path / "outside.lock"
    outside.write_text("keep")
    os.chmod(outside, 0o600)
    os.link(outside, runtime / "instance.lock")

    with pytest.raises(StartupError, match="锁"):
        acquire_instance(runtime, probe=lambda port: False)

    assert outside.read_text() == "keep"


@pytest.mark.skipif(os.name == "nt", reason="POSIX openat lock injection")
def test_lock_swap_to_symlink_is_rejected_without_locking_target(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    outside = tmp_path / "outside.lock"
    outside.write_text("keep")

    def install_lock_symlink(stage: str) -> None:
        if stage == "before_lock_open":
            (runtime / "instance.lock").symlink_to(outside)

    with pytest.raises(StartupError, match="锁"):
        acquire_instance(runtime, probe=lambda port: False, _operation_hook=install_lock_symlink)

    assert outside.read_text() == "keep"


@pytest.mark.skipif(os.name == "nt", reason="POSIX fchmod injection")
def test_permission_tightening_failure_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)

    def fail_chmod(_fd: int, _mode: int) -> None:
        raise OSError("denied")

    monkeypatch.setattr(instance_module.os, "fchmod", fail_chmod)

    with pytest.raises(StartupError, match="权限"):
        write_instance_record(runtime, _record())

    assert not (runtime / "instance.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX openat race injection")
def test_temporary_record_swap_during_publish_is_not_accepted(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    outside = tmp_path / "outside.json"
    outside.write_text("outside sentinel")

    def swap_temporary(stage: str) -> None:
        if stage != "before_record_publish":
            return
        temporary = next(runtime.glob(".instance-*"))
        temporary.unlink()
        temporary.symlink_to(outside)

    with pytest.raises(StartupError, match="发布"):
        write_instance_record(runtime, _record(), _operation_hook=swap_temporary)

    assert outside.read_text() == "outside sentinel"
    assert not (runtime / "instance.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX openat race injection")
def test_record_swap_during_cleanup_does_not_delete_replacement(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    write_instance_record(runtime, _record())
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps({"pid": 999, "port": 8768, "started_at": "2026-09-10T09:00:00Z"}))
    os.chmod(replacement, 0o600)

    def swap_record(stage: str) -> None:
        if stage != "before_record_cleanup":
            return
        (runtime / "instance.json").unlink()
        replacement.rename(runtime / "instance.json")

    with pytest.raises(StartupError, match="清理"):
        acquire_instance(runtime, probe=lambda port: False, _operation_hook=swap_record)

    assert json.loads((runtime / "instance.json").read_text())["pid"] == 999


def test_windows_backend_fails_closed_when_handle_security_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On a real Windows runner os.name is already "nt" and WinDLL exists, so
    # patching only os.name tests nothing there. Remove the API as well to
    # reach the branch this is about: refuse rather than fall back.
    monkeypatch.setattr(instance_module.os, "name", "nt")
    monkeypatch.delattr(instance_module.ctypes, "WinDLL", raising=False)

    with pytest.raises(StartupError, match="Windows.*安全.*拒绝"):
        write_instance_record(tmp_path / "runtime", _record())


@pytest.mark.skipif(os.name != "nt", reason="Windows handle and ACL validation")
def test_windows_runtime_rejects_reparse_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime = tmp_path / "runtime"
    try:
        runtime.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"creating a Windows reparse point is not permitted: {exc}")

    with pytest.raises(StartupError, match="reparse|junction|安全"):
        write_instance_record(runtime, _record())
