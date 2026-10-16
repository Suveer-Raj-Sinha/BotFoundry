"""The saved workflow format (what the editor reads/writes and the engine runs)."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float


class NodeInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)  # unique within the workflow
    type: str  # node type id, e.g. "core.condition"
    version: int = Field(default=1, ge=1)  # pinned node version
    title: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    position: Position | None = None  # editor layout only; never affects execution


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    source_port: str
    target: str
    target_port: str


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    nodes: list[NodeInstance]
    edges: list[Edge] = Field(default_factory=list)
