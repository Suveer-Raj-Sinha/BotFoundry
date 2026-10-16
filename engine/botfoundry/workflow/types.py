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


def matches_type(value: object, data_type: DataType) -> bool:
    """Does a runtime value fit a port type? (bool is not a number.)"""
    if data_type == DataType.ANY:
        return True
    if data_type == DataType.STRING:
        return isinstance(value, str)
    if data_type == DataType.NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if data_type == DataType.BOOLEAN:
        return isinstance(value, bool)
    if data_type == DataType.OBJECT:
        return isinstance(value, dict)
    return isinstance(value, list)  # DataType.LIST
