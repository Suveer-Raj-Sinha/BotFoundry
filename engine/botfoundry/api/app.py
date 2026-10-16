from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .. import __version__
from ..config import Settings, TOKEN_HEADER
from .routes_health import router as health_router


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="BotFoundry Engine", version=__version__, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=[TOKEN_HEADER, "Content-Type"],
    )
    app.include_router(health_router)
    return app
