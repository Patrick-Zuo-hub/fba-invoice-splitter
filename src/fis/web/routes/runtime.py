"""Page heartbeat and best-effort page-closed signals feeding shutdown."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from starlette.responses import Response

from fis.runtime.lifecycle import Lifecycle

router = APIRouter()


def _lifecycle(request: Request) -> Lifecycle:
    lifecycle = request.app.state.lifecycle
    assert isinstance(lifecycle, Lifecycle)
    return lifecycle


async def _page_id(request: Request) -> str | None:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed body is a client error
        return None
    if not isinstance(payload, dict):
        return None
    page_id = payload.get("page_id")
    return page_id if isinstance(page_id, str) and page_id else None


@router.post("/runtime/heartbeat")
async def heartbeat(request: Request) -> Response:
    page_id = await _page_id(request)
    if page_id is None:
        return PlainTextResponse("缺少 page_id", status_code=400)
    _lifecycle(request).heartbeat(page_id)
    return Response(status_code=204)


@router.post("/runtime/page-closed")
async def page_closed(request: Request) -> Response:
    page_id = await _page_id(request)
    if page_id is None:
        return PlainTextResponse("缺少 page_id", status_code=400)
    _lifecycle(request).remove_page(page_id)
    return Response(status_code=204)


__all__ = ["router"]
