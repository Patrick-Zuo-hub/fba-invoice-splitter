from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

from fis.runtime.instance import (
    ExistingInstance,
    InstanceRecord,
    StartupError,
    _acquire_windows_instance,
    _WindowsFileInfo,
    _WindowsRuntimeDirectory,
)


def _record(pid: int = 123) -> InstanceRecord:
    return InstanceRecord(pid=pid, port=8768, started_at="2026-09-10T09:00:00+00:00")


class FakeWindowsApi:
    def __init__(self) -> None:
        self.next_handle = 10
        self.handles: dict[int, str] = {}
        self.files: dict[str, bytearray] = {}
        self.identities: dict[str, tuple[int, int]] = {}
        self.final_paths: dict[str, str] = {}
        self.reparse: set[str] = set()
        self.bad_acl: set[str] = set()
        self.hardlinks: set[str] = set()
        self.closed: list[int] = []
        self.locked: set[str] = set()
        self.fail_operation: str | None = None
        self.swap_runtime_on_verify = False

    @staticmethod
    def normalize(path: Path | str) -> str:
        return str(PureWindowsPath(path)).casefold()

    def _identity(self, path: str) -> tuple[int, int]:
        if path not in self.identities:
            self.identities[path] = (7, len(self.identities) + 100)
        return self.identities[path]

    def validate_local_path(self, path: Path) -> Path:
        text = self.normalize(path)
        if text.startswith("\\\\"):
            raise StartupError("Windows 运行目录必须位于本机固定磁盘")
        return Path(str(PureWindowsPath(path)))

    def create_directory(self, path: Path) -> None:
        self.files.setdefault(self.normalize(path) + "\\", bytearray())

    def open_directory(self, path: Path) -> tuple[int, _WindowsFileInfo]:
        return self._open(path, directory=True)

    def open_file(self, path: Path, *, access: str, creation: str) -> tuple[int, _WindowsFileInfo]:
        key = self.normalize(path)
        if creation == "create_new" and key in self.files:
            raise FileExistsError(path)
        if creation == "open_existing" and key not in self.files:
            raise FileNotFoundError(path)
        if creation in {"create_new", "open_always"}:
            self.files.setdefault(key, bytearray())
        return self._open(path, directory=False)

    def _open(self, path: Path, *, directory: bool) -> tuple[int, _WindowsFileInfo]:
        if self.fail_operation == "open":
            raise OSError("injected open failure")
        key = self.normalize(path)
        if self.swap_runtime_on_verify and key.endswith("runtime"):
            self.identities[key] = (7, 9999)
            self.swap_runtime_on_verify = False
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = key
        info = _WindowsFileInfo(
            identity=self._identity(key),
            final_path=self.final_paths.get(key, key),
            is_directory=directory,
            is_reparse=key in self.reparse,
            link_count=2 if key in self.hardlinks else 1,
            size=len(self.files.get(key, b"")),
            acl_safe=key not in self.bad_acl,
        )
        return handle, info

    def info(self, handle: int) -> _WindowsFileInfo:
        key = self.handles[handle]
        directory = key.endswith("runtime")
        return _WindowsFileInfo(
            identity=self._identity(key),
            final_path=self.final_paths.get(key, key),
            is_directory=directory,
            is_reparse=key in self.reparse,
            link_count=2 if key in self.hardlinks else 1,
            size=len(self.files.get(key, b"")),
            acl_safe=key not in self.bad_acl,
        )

    def read(self, handle: int, limit: int) -> bytes:
        return bytes(self.files[self.handles[handle]][:limit])

    def write(self, handle: int, payload: bytes) -> None:
        self.files[self.handles[handle]] = bytearray(payload)

    def flush(self, handle: int) -> None:
        if self.fail_operation == "flush":
            raise OSError("injected flush failure")

    def replace(self, source: Path, target: Path) -> None:
        source_key, target_key = self.normalize(source), self.normalize(target)
        self.files[target_key] = self.files.pop(source_key)
        self.identities[target_key] = self.identities.pop(source_key)

    def delete(self, path: Path, expected: tuple[int, int], expected_parent: str) -> None:
        key = self.normalize(path)
        if key in self.reparse or key in self.hardlinks or self._identity(key) != expected:
            raise StartupError("实例记录在清理时被替换")
        if (
            self.normalize(PureWindowsPath(self.final_paths.get(key, key)).parent)
            != expected_parent
        ):
            raise StartupError("实例记录在清理时被替换")
        self.files.pop(key, None)

    def lock(self, handle: int) -> bool:
        key = self.handles[handle]
        if key in self.locked:
            return False
        self.locked.add(key)
        return True

    def unlock(self, handle: int) -> None:
        if self.fail_operation == "unlock":
            raise OSError("injected unlock failure")
        self.locked.discard(self.handles[handle])

    def close(self, handle: int) -> None:
        self.closed.append(handle)
        self.handles.pop(handle, None)


def _directory(api: FakeWindowsApi) -> _WindowsRuntimeDirectory:
    return _WindowsRuntimeDirectory.open(
        Path("C:/Users/me/FIS/runtime"), None, create=True, api=api
    )


def test_fake_windows_record_roundtrip_and_atomic_replacement() -> None:
    api = FakeWindowsApi()
    directory = _directory(api)
    first = directory.write_record(_record())
    second = directory.write_record(_record(456))

    assert first != second
    assert directory.read_record()[0] == _record(456)
    assert not any(".instance-" in key for key in api.files)
    directory.close()
    assert not api.handles


@pytest.mark.parametrize("unsafe", ["reparse", "acl", "hardlink", "parent"])
def test_fake_windows_rejects_unsafe_record_without_reading(unsafe: str) -> None:
    api = FakeWindowsApi()
    directory = _directory(api)
    key = api.normalize(directory.path / "instance.json")
    api.files[key] = bytearray(b'{"pid":123,"port":8768,"started_at":"2026-09-10T09:00:00Z"}')
    if unsafe == "reparse":
        api.reparse.add(key)
    elif unsafe == "acl":
        api.bad_acl.add(key)
    elif unsafe == "hardlink":
        api.hardlinks.add(key)
    else:
        api.final_paths[key] = api.normalize(Path("C:/outside/instance.json"))

    with pytest.raises(Exception, match="reparse|权限|hard link|运行目录"):
        directory.read_record()
    directory.close()
    assert not api.handles


def test_fake_windows_directory_identity_swap_fails_closed_and_closes_handles() -> None:
    api = FakeWindowsApi()
    directory = _directory(api)
    api.swap_runtime_on_verify = True

    with pytest.raises(StartupError, match="运行目录已被替换"):
        directory.write_record(_record())
    directory.close()
    assert not api.handles


def test_fake_windows_temporary_reparse_is_never_published_or_deleted() -> None:
    api = FakeWindowsApi()
    temporary_key = ""

    def replace_temporary(stage: str) -> None:
        nonlocal temporary_key
        if stage == "before_record_publish":
            temporary_key = next(key for key in api.files if ".instance-" in key)
            api.reparse.add(temporary_key)

    directory = _WindowsRuntimeDirectory.open(
        Path("C:/Users/me/FIS/runtime"), replace_temporary, create=True, api=api
    )
    with pytest.raises(Exception, match="reparse"):
        directory.write_record(_record())

    assert temporary_key in api.files
    assert api.normalize(directory.path / "instance.json") not in api.files
    directory.close()


def test_fake_windows_cleanup_identity_swap_preserves_replacement() -> None:
    api = FakeWindowsApi()
    record_key = api.normalize(Path("C:/Users/me/FIS/runtime/instance.json"))

    def swap_record(stage: str) -> None:
        if stage == "before_record_cleanup":
            api.identities[record_key] = (7, 9876)

    directory = _WindowsRuntimeDirectory.open(
        Path("C:/Users/me/FIS/runtime"), swap_record, create=True, api=api
    )
    identity = directory.write_record(_record())
    with pytest.raises(StartupError, match="清理"):
        directory.remove_record(identity)

    assert record_key in api.files
    directory.close()


def test_fake_windows_rejects_unc_runtime_path() -> None:
    api = FakeWindowsApi()
    with pytest.raises(StartupError, match="固定磁盘"):
        _WindowsRuntimeDirectory.open(Path("//server/share/runtime"), None, create=True, api=api)
    assert not api.handles


def test_fake_windows_rejects_runtime_resolved_through_swapped_ancestor() -> None:
    api = FakeWindowsApi()
    runtime = Path("C:/Users/me/FIS/runtime")
    api.final_paths[api.normalize(runtime)] = api.normalize(
        Path("C:/outside/FIS/runtime")
    )

    with pytest.raises(StartupError, match="路径.*重定向"):
        _WindowsRuntimeDirectory.open(runtime, None, create=True, api=api)

    assert not api.handles


def test_fake_windows_lock_busy_probes_record_and_preserves_owner_lock() -> None:
    api = FakeWindowsApi()
    runtime = Path("C:/Users/me/FIS/runtime")
    owner = _acquire_windows_instance(runtime, probe=lambda _port: False, api=api)
    assert not isinstance(owner, ExistingInstance)
    owner.write_record(_record())

    contender = _acquire_windows_instance(runtime, probe=lambda port: port == 8768, api=api)
    assert contender == ExistingInstance(8768)
    assert api.locked
    owner.close()
    assert not api.locked
    assert not api.handles


def test_fake_windows_bad_directory_acl_and_api_failure_close_handles() -> None:
    api = FakeWindowsApi()
    runtime_key = api.normalize(Path("C:/Users/me/FIS/runtime"))
    api.bad_acl.add(runtime_key)
    with pytest.raises(StartupError, match="权限"):
        _directory(api)
    assert not api.handles

    api = FakeWindowsApi()
    directory = _directory(api)
    api.fail_operation = "flush"
    with pytest.raises(OSError, match="flush"):
        directory.write_record(_record())
    directory.close()
    assert not api.handles


def test_fake_windows_unlock_failure_still_closes_every_handle() -> None:
    api = FakeWindowsApi()
    lease = _acquire_windows_instance(
        Path("C:/Users/me/FIS/runtime"), probe=lambda _port: False, api=api
    )
    assert not isinstance(lease, ExistingInstance)
    api.fail_operation = "unlock"

    with pytest.raises(OSError, match="unlock"):
        lease.close()

    assert not api.handles


@pytest.mark.skipif(__import__("os").name != "nt", reason="real Windows integration")
def test_real_windows_lock_and_record_roundtrip(tmp_path: Path) -> None:
    from fis.runtime.instance import acquire_instance, read_instance_record

    runtime = tmp_path / "runtime-长路径"
    owner = acquire_instance(runtime, probe=lambda _port: False)
    owner.write_record(_record())
    owner.write_record(_record(456))
    try:
        assert read_instance_record(runtime) == _record(456)
        assert acquire_instance(runtime, probe=lambda port: port == 8768) == ExistingInstance(8768)
    finally:
        owner.close()
    assert not (runtime / "instance.json").exists()
