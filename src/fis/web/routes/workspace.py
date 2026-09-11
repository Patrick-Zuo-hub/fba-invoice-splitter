"""One-page upload, parse, and summary routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.responses import Response

from fis.services import batches
from fis.services.batches import BatchService, UploadRejected, WorkspaceBatch
from fis.xlsx.errors import WorkbookError

router = APIRouter()

_UPLOAD_CHUNK = 1024 * 1024


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
            raise UploadRejected(413, "发票文件超过 50 MiB 上限")
        chunks.append(chunk)
    return chunks


def _summary_response(request: Request, batch: WorkspaceBatch) -> Response:
    return _templates(request).TemplateResponse(
        request=request,
        name="_invoice_summary.html",
        context={
            "batch": batch,
            "shipment_count": batch.shipment_count,
            "shipments": batch.parsed.shipments,
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
        batch = await run_in_threadpool(_service(request).create_from_upload, filename, chunks)
    except UploadRejected as rejected:
        return PlainTextResponse(rejected.message, status_code=rejected.status_code)
    except WorkbookError as error:
        return PlainTextResponse(str(error) or "发票解析失败", status_code=422)
    return _summary_response(request, batch)


__all__ = ["router"]
