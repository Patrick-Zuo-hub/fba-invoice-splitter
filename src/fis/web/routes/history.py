"""Read-only history listing and safe open-folder actions."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from fis.platform.folders import PlatformError
from fis.services.batches import BatchService

router = APIRouter()


def _service(request: Request) -> BatchService:
    service = request.app.state.batch_service
    assert isinstance(service, BatchService)
    return service


def _templates(request: Request) -> Jinja2Templates:
    templates = request.app.state.templates
    assert isinstance(templates, Jinja2Templates)
    return templates


@router.get("/history")
def list_history(request: Request) -> Response:
    entries = _service(request).history_entries()
    return _templates(request).TemplateResponse(
        request=request, name="_history.html", context={"entries": entries}
    )


@router.post("/history/{batch_id}/open-folder")
async def open_history_folder(request: Request, batch_id: str) -> Response:
    service = _service(request)
    try:
        await run_in_threadpool(service.open_history_folder, batch_id)
    except KeyError:
        return PlainTextResponse("未找到该批次", status_code=404)
    except PlatformError as error:
        return PlainTextResponse(str(error) or "文件已移动或删除", status_code=422)
    return Response(status_code=204)


__all__ = ["router"]
