from fastapi import Request

from ..run_manager import RunManager
from ..storage.repository import Storage
from ..workflow.registry import NodeRegistry
from .errors import ApiError


def get_registry(request: Request) -> NodeRegistry:
    return request.app.state.registry


def get_storage(request: Request) -> Storage:
    storage = request.app.state.storage
    if storage is None:
        raise ApiError(503, "storage_unavailable", "This engine was started without a database.")
    return storage


def get_manager(request: Request) -> RunManager:
    get_storage(request)
    return request.app.state.run_manager
