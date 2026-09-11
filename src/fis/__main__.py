"""Console entry point: start the local FIS server and open the browser.

Run with ``python -m fis``. The data directory (SQLite database, temporary
batch files) defaults to ``~/.fba-invoice-splitter`` and can be overridden
with the ``FIS_DATA_ROOT`` environment variable.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fis.config import Settings
from fis.runtime.server import StartupError, launch


def main() -> int:
    default_root = Path.home() / ".fba-invoice-splitter"
    data_root = Path(os.environ.get("FIS_DATA_ROOT", default_root)).expanduser()
    settings = Settings(data_root=data_root)
    try:
        result = asyncio.run(launch(settings))
    except StartupError as error:
        print(f"FIS 启动失败：{error}")
        return 1
    if result.started:
        print(f"FIS 已退出（曾运行于 http://127.0.0.1:{result.port}/）。")
    else:
        print(f"检测到已在运行的实例，已打开 http://127.0.0.1:{result.port}/。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
