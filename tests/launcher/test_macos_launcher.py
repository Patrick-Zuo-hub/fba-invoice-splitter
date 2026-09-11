from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "启动 FIS.command"


def test_launcher_is_executable_and_uses_project_virtualenv() -> None:
    assert LAUNCHER.is_file()
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR


def test_launcher_handles_project_path_with_spaces(tmp_path: Path) -> None:
    project = tmp_path / "FIS project with spaces"
    python = project / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    capture = tmp_path / "capture.txt"
    shutil.copy2(LAUNCHER, project / LAUNCHER.name)
    python.write_text(
        "#!/bin/zsh\nprint -r -- \"$PWD\" > \"$FIS_LAUNCH_CAPTURE\"\n"
        "print -r -- \"$PYTHONPATH\" >> \"$FIS_LAUNCH_CAPTURE\"\n"
        "print -r -- \"$*\" >> \"$FIS_LAUNCH_CAPTURE\"\n",
        encoding="utf-8",
    )
    python.chmod(0o700)

    environment = dict(os.environ, FIS_LAUNCH_CAPTURE=str(capture))
    result = subprocess.run(
        [str(project / LAUNCHER.name)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert capture.read_text(encoding="utf-8").splitlines() == [
        str(project),
        str(project / "src"),
        "-m fis",
    ]


def test_launcher_explains_missing_virtualenv(tmp_path: Path) -> None:
    project = tmp_path / "empty project"
    project.mkdir()
    shutil.copy2(LAUNCHER, project / LAUNCHER.name)

    result = subprocess.run(
        [str(project / LAUNCHER.name)], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "未找到项目运行环境" in result.stdout
