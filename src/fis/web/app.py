from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fis.config import Settings
from fis.web.security import LocalRequestMiddleware, LocalRequestPolicy, secure_response

_WEB_ROOT = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=_WEB_ROOT / "templates")


def create_app(settings: Settings, *, bound_port: int) -> FastAPI:
    policy = LocalRequestPolicy(bound_port)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.bound_port = bound_port
    app.add_middleware(LocalRequestMiddleware, policy=policy)
    app.mount("/static", StaticFiles(directory=_WEB_ROOT / "static"), name="static")

    @app.exception_handler(Exception)
    async def unhandled_exception(_request: Request, _exc: Exception) -> Response:
        return secure_response(PlainTextResponse("内部服务器错误", status_code=500))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
    def index(request: Request) -> Response:
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="base.html",
        )

    return app
