import os
import subprocess
import sys
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


def test_powershell_does_not_append_the_path_after_a_command_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """powershell -Command joins everything after it into the command itself.

    An argument placed there never reaches $args; it is concatenated onto the
    script text, so the dialog opened with no initial directory and PowerShell
    then tried to run the path as a command.
    """

    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return _completed(str(tmp_path))

    monkeypatch.setattr(folders.subprocess, "run", fake_run)

    folders.choose_with_powershell(tmp_path)

    args = captured["args"]
    assert isinstance(args, list)
    command_index = args.index("-Command")
    assert command_index == len(args) - 2, "-Command 后只能跟脚本本身"
    assert str(tmp_path) not in args


def test_powershell_passes_the_initial_directory_through_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not interpolated into the script either: a path must never become code."""

    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return _completed(str(tmp_path))

    monkeypatch.setattr(folders.subprocess, "run", fake_run)

    folders.choose_with_powershell(tmp_path)

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment[folders.INITIAL_DIRECTORY_VARIABLE] == str(tmp_path)
    args = captured["args"]
    assert isinstance(args, list)
    assert all(str(tmp_path) not in argument for argument in args)


def test_powershell_clears_a_stale_initial_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise an inherited value would silently steer the dialog."""

    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["env"] = kwargs.get("env")
        return _completed(str(tmp_path))

    monkeypatch.setenv(folders.INITIAL_DIRECTORY_VARIABLE, "C:\\stale")
    monkeypatch.setattr(folders.subprocess, "run", fake_run)

    folders.choose_with_powershell(None)

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert folders.INITIAL_DIRECTORY_VARIABLE not in environment


def test_powershell_reads_its_output_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Chinese folder name comes back mangled under the console code page."""

    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["encoding"] = kwargs.get("encoding")
        return _completed(str(tmp_path))

    monkeypatch.setattr(folders.subprocess, "run", fake_run)

    folders.choose_with_powershell(None)

    assert captured["encoding"] == "utf-8"
    assert "[Console]::OutputEncoding" in folders._WINDOWS_SCRIPT


@pytest.mark.skipif(sys.platform != "win32", reason="real PowerShell invocation")
def test_powershell_really_receives_the_directory_and_returns_utf8(
    tmp_path: Path,
) -> None:
    """Drive real PowerShell the way the picker does, minus the dialog.

    Everything this exercises — that -Command leaves the environment intact,
    that the script can read the variable, and that a non-ASCII path survives
    the round trip — is invisible on a developer machine.
    """

    chinese = tmp_path / "输出文件夹 测试"
    chinese.mkdir()
    probe = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        f"$start = [Environment]::GetEnvironmentVariable('{folders.INITIAL_DIRECTORY_VARIABLE}')\n"
        "if ($start -and (Test-Path -LiteralPath $start)) "
        "{ [Console]::Out.Write($start) } else { [Console]::Out.Write('MISSING') }\n"
    )
    environment = dict(os.environ)
    environment[folders.INITIAL_DIRECTORY_VARIABLE] = str(chinese)

    completed = subprocess.run(
        [folders.powershell_executable(), "-NoProfile", "-STA", "-Command", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == str(chinese)
    assert folders._validate_directory(completed.stdout) == chinese
