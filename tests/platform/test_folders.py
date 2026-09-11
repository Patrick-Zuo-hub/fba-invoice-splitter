import subprocess
from pathlib import Path

import pytest

from fis.platform import folders


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["osascript"], returncode=returncode, stdout=stdout, stderr="")


def test_osascript_returns_selected_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        return _completed(f"{tmp_path}\n")

    monkeypatch.setattr(folders.subprocess, "run", fake_run)

    result = folders.choose_with_osascript(tmp_path)

    assert result == tmp_path
    assert captured["args"][0] == "/usr/bin/osascript"
    assert str(tmp_path) in captured["args"]


def test_osascript_passes_initial_path_as_argument_not_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        return _completed(f"{tmp_path}\n")

    monkeypatch.setattr(folders.subprocess, "run", fake_run)
    folders.choose_with_osascript(tmp_path)

    script_index = captured["args"].index("-e") + 1
    script_text = captured["args"][script_index]
    assert str(tmp_path) not in script_text
    assert captured["args"][-1] == str(tmp_path)


def test_osascript_cancel_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(folders.subprocess, "run", lambda *a, **k: _completed("\n"))

    assert folders.choose_with_osascript(None) is None


def test_osascript_command_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(folders.subprocess, "run", lambda *a, **k: _completed("", returncode=1))

    with pytest.raises(folders.PlatformError):
        folders.choose_with_osascript(None)


def test_selected_path_must_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing"
    monkeypatch.setattr(folders.subprocess, "run", lambda *a, **k: _completed(str(missing)))

    with pytest.raises(folders.PlatformError):
        folders.choose_with_osascript(None)


def test_symlinked_directory_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(folders.subprocess, "run", lambda *a, **k: _completed(str(link)))

    with pytest.raises(folders.PlatformError):
        folders.choose_with_osascript(None)


def test_native_picker_rejects_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(folders.sys, "platform", "linux")
    monkeypatch.setattr(folders.os, "name", "posix")

    with pytest.raises(folders.PlatformError):
        folders.NativeFolderPicker().choose(None)
