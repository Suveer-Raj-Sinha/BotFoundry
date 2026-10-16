from enum import Enum

from pydantic import BaseModel, ConfigDict


class DataType(str, Enum):
    """Types a port can carry. ANY is compatible with everything."""

    ANY = "any"
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    OBJECT = "object"
    LIST = "list"


def is_compatible(source: DataType, target: DataType) -> bool:
    """Can data from a `source` port flow into a `target` port?"""
    return source == target or DataType.ANY in (source, target)


class PortSpec(BaseModel):
    """One input or output of a node."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: DataType = DataType.ANY
    required: bool = True  # only meaningful for inputs
    description: str = ""
