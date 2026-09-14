from __future__ import annotations

import asyncio
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Locator, Page, sync_playwright

from fis.config import Settings
from fis.storage.repositories import MasterDataRepository
from fis.web.app import create_app
from tests.fixtures.standard_v1_builder import build_standard_v1


class FixedFolderPicker:
    def __init__(self, output: Path) -> None:
        self.output = output

    def choose(self, initial: Path | None) -> Path:
        return self.output


@dataclass(frozen=True)
class BrowserApp:
    app: FastAPI
    url: str
    source: Path
    broken: Path
    output: Path


@pytest.fixture
def browser_app(tmp_path: Path) -> BrowserApp:
    output = tmp_path / "output"
    output.mkdir()
    source = tmp_path / "source.xlsx"
    build_standard_v1(source, {"S1": 2, "S2": 1}, [12, 13, 14])
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"not-a-zip")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    app = create_app(
        Settings(data_root=tmp_path / "data"),
        bound_port=port,
        folder_picker=FixedFolderPicker(output),
    )
    masters = MasterDataRepository(app.state.database)
    masters.add_channel("美森正班")
    masters.add_channel("空运")
    # Latin-script too, so the case-insensitive filter is testable.
    masters.add_channel("MATSON Express")
    masters.add_delivery_warehouse("义乌仓", "浙江省义乌市测试路 1 号")
    masters.create_fba_warehouse("ONT8", "Ontario, CA 91761")

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", log_level="critical", lifespan="off")
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        raise RuntimeError("browser test server did not start")

    try:
        yield BrowserApp(app, f"http://127.0.0.1:{port}", source, broken, output)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


@pytest.fixture
def page(browser_app: BrowserApp) -> Page:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.goto(browser_app.url)
        try:
            yield page
        finally:
            context.close()
            browser.close()


def upload(page: Page, path: Path) -> None:
    page.locator("#invoice").set_input_files(path)
    page.get_by_role("button", name="上传并解析").click()


def choose_channel(row: Locator, name: str) -> None:
    """Pick a channel from the filtered list, the way a user does."""

    row.locator("[data-combo-input]").click()
    row.locator("[data-combo-list] li", has_text=name).first.click()


def apply_mapping(page: Page, text: str) -> None:
    page.get_by_role("textbox", name="从 Excel 粘贴两列内容：仓库代码在前，货件单号在后").fill(text)
    with page.expect_response(
        lambda response: response.url.endswith("/mapping")
        and response.request.method == "POST"
    ):
        page.get_by_role("button", name="应用映射").click()
