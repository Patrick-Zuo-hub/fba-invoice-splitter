# macOS Double-Click Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a project-local `启动 FIS.command` file that starts the existing development environment from Finder without a manually entered terminal command.

**Architecture:** A small zsh launcher resolves the directory containing itself, validates the project virtual-environment Python, changes into the project root, sets `PYTHONPATH` to the local `src` directory, and delegates to `python -m fis`. Pytest executes the launcher against a fake project in a path containing spaces so path handling and failure messages remain deterministic without opening a browser during the automated test.

**Tech Stack:** zsh, Python 3.12, pytest, existing `fis.__main__` launcher

---

## File structure

| File | Responsibility |
| --- | --- |
| `启动 FIS.command` | Finder-double-clickable macOS entry point; contains no application logic. |
| `tests/launcher/test_macos_launcher.py` | Verifies executable metadata, project-relative launch behavior, path quoting, and missing-environment failure. |

### Task 1: Add and test the project-relative launcher

**Files:**
- Create: `tests/launcher/__init__.py`
- Create: `tests/launcher/test_macos_launcher.py`
- Create: `启动 FIS.command`

- [ ] **Step 1: Write the failing launcher tests**

```python
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
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `PYTHONPATH=.:src .venv/bin/pytest tests/launcher/test_macos_launcher.py -v`

Expected: FAIL because `启动 FIS.command` does not exist.

- [ ] **Step 3: Implement the minimal launcher**

Create `启动 FIS.command` with:

```zsh
#!/bin/zsh
set -u

SCRIPT_DIR="${0:A:h}"
PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  print -r -- "FIS 启动失败：未找到项目运行环境 .venv/bin/python。"
  print -r -- "请先完成开发环境配置，再重新双击此文件。"
  if [[ -t 0 ]]; then
    print -n -- "按回车键关闭此窗口…"
    read -r
  fi
  exit 1
fi

cd "$SCRIPT_DIR" || exit 1
export PYTHONPATH="$SCRIPT_DIR/src"
exec "$PYTHON_BIN" -m fis
```

Then make it executable:

```bash
chmod 755 "启动 FIS.command"
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `PYTHONPATH=.:src .venv/bin/pytest tests/launcher/test_macos_launcher.py -v`

Expected: 3 tests pass.

- [ ] **Step 5: Commit the launcher**

```bash
git add "启动 FIS.command" tests/launcher
git commit -m "feat: add macOS double-click launcher"
```

### Task 2: Validate Finder-equivalent launch and regressions

**Files:**
- Modify only if a verified defect requires it: `启动 FIS.command`
- Modify only if a verified defect requires it: `tests/launcher/test_macos_launcher.py`

- [ ] **Step 1: Run quality and regression checks**

Run: `.venv/bin/ruff check src tests`

Expected: all checks pass.

Run: `.venv/bin/mypy src`

Expected: no issues found.

Run: `PYTHONPATH=.:src .venv/bin/pytest -q`

Expected: the complete suite passes, with only documented platform-specific skips.

- [ ] **Step 2: Perform Finder-equivalent launch**

Run: `open "启动 FIS.command"`

Expected: Terminal executes the launcher and the browser opens an FIS page at the first available port in `8765–8775`.

- [ ] **Step 3: Confirm live local service**

Resolve the selected port from the running Python process and request `/health`.

Expected response:

```json
{"status":"ok"}
```

- [ ] **Step 4: Close the test page and confirm automatic exit**

Close the page opened in Step 2 and wait for the existing 30-second shutdown behavior.

Expected: the FIS Python process exits without manual termination.

- [ ] **Step 5: Verify repository state**

Run: `git status --short`

Expected: no uncommitted files.
