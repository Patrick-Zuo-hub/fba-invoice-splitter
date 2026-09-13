from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "启动 FIS.bat"

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows batch execution")


def _script() -> str:
    return LAUNCHER.read_text(encoding="utf-8")


def test_launcher_exists_at_the_project_root() -> None:
    assert LAUNCHER.is_file()


def test_launcher_locates_the_project_from_its_own_directory() -> None:
    """A hard-coded user or worktree path would break on every other machine."""

    script = _script()

    assert "%~dp0" in script
    assert "C:\\Users" not in script
    assert "/Users/" not in script


def test_launcher_uses_the_project_virtualenv_and_source_path() -> None:
    script = _script()

    assert r".venv\Scripts\python.exe" in script
    assert "PYTHONPATH" in script
    assert "-m fis" in script


def test_launcher_switches_console_to_utf8_for_chinese_output() -> None:
    """Without UTF-8 the Chinese prompts render as mojibake in cmd.exe."""

    assert "chcp 65001" in _script()


def test_launcher_explains_a_missing_virtualenv_in_chinese() -> None:
    assert "未找到项目运行环境" in _script()


@windows_only
def test_launcher_passes_working_directory_and_environment(tmp_path: Path) -> None:
    """Drive the real launcher against a real venv and a stand-in ``fis`` module.

    A batch file cannot stand in for ``python.exe`` — cmd resolves executables
    by extension — so the interpreter is genuine and only the imported package
    is replaced, which is exactly what ``PYTHONPATH`` is expected to control.
    """

    project = tmp_path / "FIS project with spaces"
    project.mkdir()
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(project / ".venv")],
        check=True,
        capture_output=True,
    )
    capture = tmp_path / "capture.txt"
    package = project / "src" / "fis"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import os, sys, pathlib\n"
        "pathlib.Path(os.environ['FIS_LAUNCH_CAPTURE']).write_text(\n"
        "    '\\n'.join([os.getcwd(), os.environ.get('PYTHONPATH', ''), ' '.join(sys.argv[1:])]),\n"
        "    encoding='utf-8',\n"
        ")\n",
        encoding="utf-8",
    )
    shutil.copy2(LAUNCHER, project / LAUNCHER.name)

    environment = dict(os.environ, FIS_LAUNCH_CAPTURE=str(capture))
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(project / LAUNCHER.name)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    recorded = capture.read_text(encoding="utf-8").split("\n")
    assert recorded == [str(project), str(project / "src"), ""]


@windows_only
def test_launcher_fails_without_a_virtualenv(tmp_path: Path) -> None:
    project = tmp_path / "empty project"
    project.mkdir()
    shutil.copy2(LAUNCHER, project / LAUNCHER.name)

    # DEVNULL rather than input=: `pause` returns at once on a closed stdin,
    # and the encoding is pinned to match the launcher's `chcp 65001`.
    result = subprocess.run(
        [str(project / LAUNCHER.name)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode != 0
    assert "未找到项目运行环境" in (result.stdout or ""), repr(result.stdout)


def test_launcher_keeps_chinese_text_out_of_a_parsed_block() -> None:
    """cmd parses a parenthesised block as one unit, which mangles UTF-8 there.

    Routing the failure branch through a label keeps every non-ASCII line at
    statement level, where the active code page applies cleanly.
    """

    script = _script()

    assert "goto :missing_venv" in script
    assert ":missing_venv" in script


def test_launcher_keeps_crlf_line_endings_after_checkout() -> None:
    """cmd is unreliable with bare LF, and git rewrites endings without a rule."""

    assert b"\r\n" in LAUNCHER.read_bytes()
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat text eol=crlf" in attributes
