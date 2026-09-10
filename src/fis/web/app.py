from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fis.config import Settings
from fis.web.security import LocalRequestMiddleware

_WEB_ROOT = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=_WEB_ROOT / "templates")


def create_app(settings: Settings, launch_secret: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.launch_secret = launch_secret
    app.add_middleware(LocalRequestMiddleware, launch_secret=launch_secret)
    app.mount("/static", StaticFiles(directory=_WEB_ROOT / "static"), name="static")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="base.html",
            context={"csrf_token": request.state.csrf_token},
        )

    return app
