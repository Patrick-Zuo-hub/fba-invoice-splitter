from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fis.config import Settings
from fis.services.batches import BatchService, WorkspaceStore
from fis.storage.batch_files import BatchFiles
from fis.web.routes import workspace as workspace_routes
from fis.web.security import (
    LocalRequestMiddleware,
    LocalRequestPolicy,
    SanitizedErrorMiddleware,
)

_WEB_ROOT = Path(__file__).resolve().parent


def create_app(settings: Settings, *, bound_port: int) -> FastAPI:
    policy = LocalRequestPolicy(bound_port)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.bound_port = bound_port

    templates = Jinja2Templates(directory=_WEB_ROOT / "templates")
    app.state.templates = templates
    app.state.batch_service = BatchService(BatchFiles(settings.data_root), WorkspaceStore())

    app.add_middleware(LocalRequestMiddleware, policy=policy)
    app.add_middleware(SanitizedErrorMiddleware)
    app.mount("/static", StaticFiles(directory=_WEB_ROOT / "static"), name="static")
    app.include_router(workspace_routes.router)

    @app.get("/health")
    def health(response: Response) -> dict[str, str]:
        response.headers["X-FIS-Product"] = "fba-invoice-splitter"
        return {"status": "ok"}

    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
    def index(request: Request) -> Response:
        return templates.TemplateResponse(request=request, name="workspace.html")

    return app
