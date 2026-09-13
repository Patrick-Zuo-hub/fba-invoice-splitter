"""Console entry point: start the local FIS server and open the browser.

Run with ``python -m fis``. In development the data directory defaults to
``~/.fba-invoice-splitter``; a packaged build keeps it beside the executable.
``FIS_DATA_ROOT`` overrides either. See ``fis.runtime.layout``.
"""

from __future__ import annotations

import asyncio

from fis.runtime.layout import resolve_layout
from fis.runtime.server import StartupError, launch


def main() -> int:
    try:
        settings = resolve_layout()
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
