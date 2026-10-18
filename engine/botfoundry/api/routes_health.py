import sys
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from .. import __version__
from .security import require_token

router = APIRouter(dependencies=[Depends(require_token)])


class HealthResponse(BaseModel):
    status: str
    engine_version: str
    python_version: str
    ai_available: bool
    database_schema_version: int | None = None


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    storage = request.app.state.storage
    # ai_available stays False until the optional AI adapter exists (Phase 6).
    return HealthResponse(
        status="ok",
        engine_version=__version__,
        python_version=sys.version.split()[0],
        ai_available=False,
        database_schema_version=storage.schema_version if storage else None,
    )
