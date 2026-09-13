"""Native output-folder selection for macOS and Windows.

The adapters open the operating system's own folder chooser through a fixed
script. The initial directory is always passed as a separate process
argument, never interpolated into executable script text, so a path can
never be treated as code. Every returned path is validated as an absolute,
existing, non-symlinked local directory before it is accepted.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol

_OSASCRIPT = "/usr/bin/osascript"

_MAC_SCRIPT = (
    "on run argv\n"
    '    set startPath to ""\n'
    "    if (count of argv) > 0 then set startPath to item 1 of argv\n"
    "    try\n"
    '        if startPath is not "" then\n'
    "            set chosen to (choose folder with prompt "
    '"选择发票输出文件夹" default location (POSIX file startPath))\n'
    "        else\n"
    '            set chosen to (choose folder with prompt "选择发票输出文件夹")\n'
    "        end if\n"
    "    on error number -128\n"
    '        return ""\n'
    "    end try\n"
    "    return POSIX path of chosen\n"
    "end run"
)

_WINDOWS_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms\n"
    "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog\n"
    "if ($args.Count -gt 0) { $dialog.SelectedPath = $args[0] }\n"
    "$result = $dialog.ShowDialog()\n"
    "if ($result -eq [System.Windows.Forms.DialogResult]::OK) "
    "{ [Console]::Out.Write($dialog.SelectedPath) }\n"
)


class PlatformError(RuntimeError):
    """A native folder-selection window could not be used safely."""


class FolderPicker(Protocol):
    def choose(self, initial: Path | None) -> Path | None:
        """Return the selected local directory, or ``None`` after cancellation."""
        raise NotImplementedError


def _validate_directory(raw: str) -> Path | None:
    text = raw.strip()
    if not text:
        return None
    if os.path.islink(text):
        raise PlatformError("不允许选择符号链接文件夹")
    path = Path(text)
    if not path.is_absolute():
        raise PlatformError("文件夹路径必须是绝对路径")
    if not path.is_dir():
        raise PlatformError("所选路径不是有效的本地文件夹")
    return path


def choose_with_osascript(initial: Path | None) -> Path | None:
    arguments = [_OSASCRIPT, "-e", _MAC_SCRIPT]
    if initial is not None:
        arguments.append(str(initial))
    completed = subprocess.run(
        arguments, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise PlatformError("无法打开文件夹选择窗口")
    return _validate_directory(completed.stdout)


def choose_with_powershell(initial: Path | None) -> Path | None:
    executable = os.path.join(
        os.environ.get("SystemRoot", r"C:\\Windows"),
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    arguments = [
        executable,
        "-NoProfile",
        "-NonInteractive",
        "-STA",
        "-Command",
        _WINDOWS_SCRIPT,
    ]
    if initial is not None:
        arguments.append(str(initial))
    completed = subprocess.run(
        arguments, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise PlatformError("无法打开文件夹选择窗口")
    return _validate_directory(completed.stdout)


class NativeFolderPicker:
    """Dispatch to the platform-native folder chooser."""

    def choose(self, initial: Path | None) -> Path | None:
        if sys.platform == "darwin":
            return choose_with_osascript(initial)
        if sys.platform == "win32":
            return choose_with_powershell(initial)
        raise PlatformError("当前系统不支持文件夹选择窗口")


class FolderOpener(Protocol):
    def open(self, folder: Path) -> None:
        """Reveal an existing local directory in the system file browser."""
        raise NotImplementedError


def open_with_system(folder: Path) -> None:
    if not folder.is_dir():
        raise PlatformError("文件已移动或删除")
    if sys.platform == "darwin":
        executable = ["/usr/bin/open", str(folder)]
    elif sys.platform == "win32":
        explorer = os.path.join(os.environ.get("SystemRoot", r"C:\\Windows"), "explorer.exe")
        executable = [explorer, str(folder)]
    else:
        raise PlatformError("当前系统不支持打开文件夹")
    completed = subprocess.run(
        executable, capture_output=True, text=True, check=False
    )
    if completed.returncode not in (0, 1):
        raise PlatformError("无法打开文件夹")


class NativeFolderOpener:
    def open(self, folder: Path) -> None:
        open_with_system(folder)


__all__ = [
    "FolderOpener",
    "FolderPicker",
    "NativeFolderOpener",
    "NativeFolderPicker",
    "PlatformError",
    "choose_with_osascript",
    "choose_with_powershell",
    "open_with_system",
]
