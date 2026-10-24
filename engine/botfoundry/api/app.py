from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .. import __version__
from ..config import Settings, TOKEN_HEADER
from ..rules.constraints import SystemConstraints, default_constraints
from ..run_manager import RunManager
from ..storage import Storage
from ..workflow.builtin_nodes import build_default_registry
from ..workflow.registry import NodeRegistry
from .errors import install_error_handlers
from .routes_health import router as health_router
from .routes_nodes import router as nodes_router
from .routes_rulebooks import router as rulebooks_router
from .routes_runs import router as runs_router
from .routes_workflows import router as workflows_router


def create_app(settings: Settings, storage: Storage | None = None, registry: NodeRegistry | None = None,
               constraints: SystemConstraints | None = None) -> FastAPI:
    registry = registry or build_default_registry()
    constraints = constraints or default_constraints()
    manager = RunManager(registry, storage, constraints=constraints) if storage is not None else None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if manager is not None:  # engine is stopping: end active runs in a recorded, terminal state
            await manager.shutdown()

    app = FastAPI(title="BotFoundry Engine", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.storage = storage
    app.state.registry = registry
    app.state.run_manager = manager
    app.state.constraints = constraints
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=[TOKEN_HEADER, "Content-Type"],
    )
    install_error_handlers(app)
    for router in (health_router, nodes_router, workflows_router, runs_router, rulebooks_router):
        app.include_router(router)
    return app
