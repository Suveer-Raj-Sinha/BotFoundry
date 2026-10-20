from fastapi import APIRouter, Depends, Request

from ..workflow.registry import NodeRegistry
from .deps import get_registry
from .schemas import NodeCatalog
from .security import require_token

router = APIRouter(dependencies=[Depends(require_token)])


@router.get("/nodes", response_model=NodeCatalog)
def list_nodes(request: Request) -> NodeCatalog:
    """Every node type the engine can run, with its config schema, ports and permissions."""
    registry: NodeRegistry = get_registry(request)
    return NodeCatalog(nodes=registry.catalog())
