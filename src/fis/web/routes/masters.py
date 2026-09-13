"""Warehouse and channel master-data management."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.responses import Response

from fis.storage.database import Database
from fis.storage.repositories import MasterDataRepository, MasterImportSummary
from fis.xlsx.errors import WorkbookError
from fis.xlsx.masters import parse_master_data

router = APIRouter()


def _repository(request: Request) -> MasterDataRepository:
    database = request.app.state.database
    assert isinstance(database, Database)
    return MasterDataRepository(database)


def _templates(request: Request) -> Jinja2Templates:
    templates = request.app.state.templates
    assert isinstance(templates, Jinja2Templates)
    return templates


def _view(repository: MasterDataRepository) -> dict[str, object]:
    return {
        "channels": repository.list_channels(),
        "delivery_warehouses": repository.list_delivery_warehouses(),
        "fba_warehouses": repository.list_fba_warehouses(),
    }


def _render(
    request: Request,
    repository: MasterDataRepository,
    status_code: int = 200,
    *,
    import_summary: str | None = None,
) -> Response:
    return _templates(request).TemplateResponse(
        request=request,
        name="_masters.html",
        context={"view": _view(repository), "import_summary": import_summary},
        status_code=status_code,
    )


async def _json(request: Request) -> dict[str, Any] | None:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON is a client error
        return None
    return payload if isinstance(payload, dict) else None


@router.get("/masters")
def list_masters(request: Request) -> Response:
    return _render(request, _repository(request))


@router.post("/masters/channels")
async def create_channel(request: Request) -> Response:
    payload = await _json(request)
    if payload is None:
        return PlainTextResponse("请求体必须是 JSON 对象", status_code=400)
    repository = _repository(request)
    try:
        await run_in_threadpool(repository.add_channel, str(payload.get("name", "")))
    except ValueError as error:
        return PlainTextResponse(str(error), status_code=422)
    return _render(request, repository)


@router.post("/masters/channels/{channel_id}")
async def update_channel(request: Request, channel_id: int) -> Response:
    payload = await _json(request)
    if payload is None:
        return PlainTextResponse("请求体必须是 JSON 对象", status_code=400)
    repository = _repository(request)
    try:
        if "name" in payload:
            await run_in_threadpool(repository.rename_channel, channel_id, str(payload["name"]))
        if "active" in payload:
            await run_in_threadpool(
                repository.set_channel_active, channel_id, bool(payload["active"])
            )
    except KeyError:
        return PlainTextResponse("未找到该渠道", status_code=404)
    except ValueError as error:
        return PlainTextResponse(str(error), status_code=422)
    return _render(request, repository)


@router.post("/masters/delivery-warehouses")
async def create_delivery_warehouse(request: Request) -> Response:
    payload = await _json(request)
    if payload is None:
        return PlainTextResponse("请求体必须是 JSON 对象", status_code=400)
    repository = _repository(request)
    try:
        await run_in_threadpool(
            repository.add_delivery_warehouse,
            str(payload.get("name", "")),
            str(payload.get("address", "")),
        )
    except ValueError as error:
        return PlainTextResponse(str(error), status_code=422)
    return _render(request, repository)


@router.post("/masters/delivery-warehouses/{warehouse_id}")
async def update_delivery_warehouse(request: Request, warehouse_id: int) -> Response:
    payload = await _json(request)
    if payload is None:
        return PlainTextResponse("请求体必须是 JSON 对象", status_code=400)
    repository = _repository(request)
    name = str(payload["name"]) if "name" in payload else None
    address = str(payload["address"]) if "address" in payload else None
    try:
        if name is not None or address is not None:
            await run_in_threadpool(
                lambda: repository.update_delivery_warehouse(
                    warehouse_id, name=name, address=address
                )
            )
        if "active" in payload:
            await run_in_threadpool(
                repository.set_delivery_warehouse_active, warehouse_id, bool(payload["active"])
            )
    except KeyError:
        return PlainTextResponse("未找到该送货仓库", status_code=404)
    except ValueError as error:
        return PlainTextResponse(str(error), status_code=422)
    return _render(request, repository)


@router.post("/masters/fba-warehouses")
async def create_fba_warehouse(request: Request) -> Response:
    payload = await _json(request)
    if payload is None:
        return PlainTextResponse("请求体必须是 JSON 对象", status_code=400)
    repository = _repository(request)
    try:
        await run_in_threadpool(
            repository.create_fba_warehouse,
            str(payload.get("code", "")),
            str(payload.get("address", "")),
        )
    except ValueError as error:
        return PlainTextResponse(str(error), status_code=422)
    except sqlite3.IntegrityError:
        return PlainTextResponse("FBA 仓库编码已存在", status_code=422)
    return _render(request, repository)


@router.post("/masters/fba-warehouses/{code}")
async def update_fba_warehouse(request: Request, code: str) -> Response:
    payload = await _json(request)
    if payload is None:
        return PlainTextResponse("请求体必须是 JSON 对象", status_code=400)
    repository = _repository(request)
    name = str(payload["name"]) if "name" in payload else None
    address = str(payload["address"]) if "address" in payload else None
    try:
        if name is not None or address is not None:
            await run_in_threadpool(
                lambda: repository.update_fba_warehouse(code, name=name, address=address)
            )
        if "active" in payload:
            await run_in_threadpool(
                repository.set_fba_warehouse_active, code, bool(payload["active"])
            )
    except KeyError:
        return PlainTextResponse("未找到该 FBA 仓库", status_code=404)
    except ValueError as error:
        return PlainTextResponse(str(error), status_code=422)
    return _render(request, repository)


__all__ = ["router"]


MAX_MASTER_UPLOAD_BYTES = 8 * 1024 * 1024
_UPLOAD_CHUNK = 64 * 1024


def _summary_text(summary: MasterImportSummary) -> str:
    return "；".join(
        f"{label}：新增 {counts.added}，更新 {counts.updated}"
        for label, counts in (
            ("FBA 仓库", summary.fba_warehouses),
            ("运输渠道", summary.channels),
            ("送货仓库", summary.delivery_warehouses),
        )
    )


@router.post("/masters/import")
async def import_masters(request: Request) -> Response:
    form = await request.form()
    uploads = [
        value
        for key, value in form.multi_items()
        if key == "masters" and isinstance(value, UploadFile)
    ]
    if len(uploads) != 1:
        return PlainTextResponse("请上传单个主数据表格", status_code=400)
    upload = uploads[0]
    if not (upload.filename or "").lower().endswith(".xlsx"):
        return PlainTextResponse("主数据表格必须是 .xlsx 文件", status_code=400)

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_MASTER_UPLOAD_BYTES:
            return PlainTextResponse("主数据表格超过 8 MiB 上限", status_code=413)
        chunks.append(chunk)

    def apply() -> MasterImportSummary:
        # Parsing needs a real path for SafePackage; the copy never outlives it.
        handle = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)  # noqa: SIM115
        temporary = Path(handle.name)
        try:
            with handle:
                for piece in chunks:
                    handle.write(piece)
            return _repository(request).import_master_data(parse_master_data(temporary))
        finally:
            temporary.unlink(missing_ok=True)

    try:
        summary = await run_in_threadpool(apply)
    except WorkbookError as error:
        return PlainTextResponse(str(error) or "主数据表格无法解析", status_code=422)
    except (ValueError, sqlite3.DatabaseError) as error:
        return PlainTextResponse(str(error) or "主数据导入失败", status_code=422)

    return _render(request, _repository(request), import_summary=_summary_text(summary))
