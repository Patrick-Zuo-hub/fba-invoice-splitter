"""One-page upload, edit, output-folder, and summary routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.responses import Response

from fis.platform.folders import PlatformError
from fis.services import batches
from fis.services.batches import BatchService, GenerationError, UploadRejected, Workspace
from fis.xlsx.errors import WorkbookError

router = APIRouter()

_UPLOAD_CHUNK = 1024 * 1024
_ALL_SUFFIX = "_all"


def _service(request: Request) -> BatchService:
    service = request.app.state.batch_service
    assert isinstance(service, BatchService)
    return service


def _templates(request: Request) -> Jinja2Templates:
    templates = request.app.state.templates
    assert isinstance(templates, Jinja2Templates)
    return templates


async def _read_bounded(upload: UploadFile, limit: int) -> list[bytes]:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise UploadRejected(413, "发票文件超过 100 MiB 上限")
        chunks.append(chunk)
    return chunks


def _summary_response(
    request: Request,
    workspace: Workspace,
    mapping_report: dict[str, object] | None = None,
) -> Response:
    service = _service(request)
    view = service.editor_view(workspace)
    view["mapping_report"] = mapping_report
    return _templates(request).TemplateResponse(
        request=request,
        name="_invoice_summary.html",
        context={
            "batch": workspace,
            "shipment_count": workspace.shipment_count,
            "shipments": workspace.parsed.shipments,
            "view": view,
        },
    )


@router.post("/workspace/invoice")
async def upload_invoice(request: Request) -> Response:
    form = await request.form()
    uploads = [
        value
        for key, value in form.multi_items()
        if key == "invoice" and isinstance(value, UploadFile)
    ]
    if len(uploads) != 1:
        return PlainTextResponse("请上传单个发票文件", status_code=400)
    upload = uploads[0]
    filename = upload.filename or ""
    try:
        chunks = await _read_bounded(upload, batches.MAX_UPLOAD_BYTES)
        workspace = await run_in_threadpool(
            _service(request).create_from_upload, filename, chunks
        )
    except UploadRejected as rejected:
        return PlainTextResponse(rejected.message, status_code=rejected.status_code)
    except WorkbookError as error:
        return PlainTextResponse(str(error) or "发票解析失败", status_code=422)
    return _summary_response(request, workspace)


@router.post("/workspace/{batch_id}/fields")
async def edit_fields(request: Request, batch_id: str) -> Response:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - any malformed JSON body is a client error
        return PlainTextResponse("请求体必须是 JSON", status_code=400)
    if not isinstance(payload, dict):
        return PlainTextResponse("请求体必须是 JSON 对象", status_code=400)

    common_raw: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "shipments":
            continue
        if isinstance(key, str) and key.endswith(_ALL_SUFFIX):
            common_raw[key[: -len(_ALL_SUFFIX)]] = value

    raw_shipments = payload.get("shipments") or {}
    if not isinstance(raw_shipments, dict):
        return PlainTextResponse("shipments 必须是对象", status_code=400)
    override_raw = {
        str(shipment_id): (fields if isinstance(fields, dict) else {})
        for shipment_id, fields in raw_shipments.items()
    }

    service = _service(request)
    try:
        workspace, errors = await run_in_threadpool(
            service.apply_fields, batch_id, common_raw, override_raw
        )
    except KeyError:
        return PlainTextResponse("未找到该批次", status_code=404)
    view = service.editor_view(workspace, errors)
    return _templates(request).TemplateResponse(
        request=request, name="_shipment_editor.html", context={"view": view}
    )


@router.post("/workspace/{batch_id}/output-folder")
async def choose_output_folder(request: Request, batch_id: str) -> Response:
    service = _service(request)
    try:
        workspace, changed = await run_in_threadpool(service.choose_output_folder, batch_id)
    except KeyError:
        return PlainTextResponse("未找到该批次", status_code=404)
    except PlatformError as error:
        return PlainTextResponse(str(error) or "所选文件夹无效", status_code=422)
    if not changed:
        return Response(status_code=204)
    return _templates(request).TemplateResponse(
        request=request,
        name="_output_folder.html",
        context={"folder": str(workspace.output_directory)},
    )


@router.post("/workspace/{batch_id}/generate")
async def generate_batch(request: Request, batch_id: str) -> Response:
    service = _service(request)
    lifecycle = request.app.state.lifecycle
    lifecycle.begin_job()
    try:
        result = await run_in_threadpool(service.generate, batch_id)
    except KeyError:
        return PlainTextResponse("未找到该批次", status_code=404)
    except GenerationError as error:
        return PlainTextResponse(error.message, status_code=error.status_code)
    finally:
        lifecycle.end_job()
    return _templates(request).TemplateResponse(
        request=request,
        name="_generation_result.html",
        context={
            "result": result,
            "filename": result.filename,
            "sheet_count": result.sheet_count,
            "output_directory": str(result.output_directory),
        },
    )


@router.patch("/workspace/{batch_id}/shipments/{shipment_id}")
async def save_shipment_cell(
    request: Request, batch_id: str, shipment_id: str
) -> Response:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON body is a client error
        return PlainTextResponse("请求体必须是 JSON", status_code=400)
    if not isinstance(payload, dict):
        return PlainTextResponse("请求体必须是 JSON 对象", status_code=400)
    field_name = str(payload.get("field", ""))
    try:
        row, errors = await run_in_threadpool(
            _service(request).save_shipment_field,
            batch_id,
            shipment_id,
            field_name,
            payload.get("value"),
        )
    except KeyError:
        return PlainTextResponse("未找到该批次或货件", status_code=404)
    except ValueError as error:
        return PlainTextResponse(str(error), status_code=422)
    view = _service(request).editor_view(_service(request).require(batch_id))
    body = {
        "save_state": "failed" if errors else "saved",
        "row": row,
        "complete_count": view["complete_count"],
        "total_count": view["total_count"],
    }
    return JSONResponse(body, status_code=422 if errors else 200)


@router.post("/workspace/{batch_id}/bulk")
async def save_bulk_field(request: Request, batch_id: str) -> Response:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON body is a client error
        return PlainTextResponse("请求体必须是 JSON", status_code=400)
    if not isinstance(payload, dict):
        return PlainTextResponse("请求体必须是 JSON 对象", status_code=400)
    service = _service(request)
    try:
        workspace, errors = await run_in_threadpool(
            service.save_bulk_field,
            batch_id,
            str(payload.get("field", "")),
            payload.get("value"),
        )
    except KeyError:
        return PlainTextResponse("未找到该批次", status_code=404)
    except ValueError as error:
        return PlainTextResponse(str(error), status_code=422)
    view = service.editor_view(workspace, errors)
    return JSONResponse(
        {
            "save_state": "failed" if errors else "saved",
            "updated": workspace.shipment_count,
            "rows": view["rows"],
            "complete_count": view["complete_count"],
            "total_count": view["total_count"],
        },
        status_code=422 if errors else 200,
    )


@router.post("/workspace/{batch_id}/shipments/{shipment_id}/warehouse-address")
async def save_inline_warehouse_address(
    request: Request, batch_id: str, shipment_id: str
) -> Response:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON body is a client error
        return PlainTextResponse("请求体必须是 JSON", status_code=400)
    if not isinstance(payload, dict):
        return PlainTextResponse("请求体必须是 JSON 对象", status_code=400)
    service = _service(request)
    try:
        row = await run_in_threadpool(
            service.save_inline_fba_warehouse,
            batch_id,
            shipment_id,
            str(payload.get("address", "")),
        )
    except KeyError:
        return PlainTextResponse("未找到该批次或货件", status_code=404)
    except ValueError as error:
        return PlainTextResponse(str(error), status_code=422)
    view = service.editor_view(service.require(batch_id))
    return JSONResponse(
        {
            "save_state": "saved",
            "row": row,
            "complete_count": view["complete_count"],
            "total_count": view["total_count"],
        }
    )


@router.post("/workspace/{batch_id}/mapping")
async def apply_mapping(request: Request, batch_id: str) -> Response:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON body is a client error
        return PlainTextResponse("请求体必须是 JSON", status_code=400)
    if not isinstance(payload, dict):
        return PlainTextResponse("请求体必须是 JSON 对象", status_code=400)
    text = str(payload.get("mapping", ""))
    service = _service(request)
    try:
        workspace, report = await run_in_threadpool(service.apply_mapping, batch_id, text)
    except KeyError:
        return PlainTextResponse("未找到该批次", status_code=404)
    except ValueError as error:
        return PlainTextResponse(f"映射解析失败：{error}", status_code=422)
    return _summary_response(request, workspace, report)


__all__ = ["router"]
