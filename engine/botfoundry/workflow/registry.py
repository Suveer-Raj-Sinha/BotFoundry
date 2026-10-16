from typing import Any

from .node import NodeSpec


class RegistryError(ValueError):
    pass


class NodeRegistry:
    """All node types the engine knows about. Nodes are looked up by (id, version)."""

    def __init__(self) -> None:
        self._specs: dict[tuple[str, int], NodeSpec] = {}

    def register(self, spec: NodeSpec) -> None:
        key = (spec.id, spec.version)
        if key in self._specs:
            raise RegistryError(f"Node {spec.id} v{spec.version} is already registered.")
        self._specs[key] = spec

    def get(self, node_id: str, version: int) -> NodeSpec | None:
        return self._specs.get((node_id, version))

    def versions(self, node_id: str) -> list[int]:
        return sorted(v for (i, v) in self._specs if i == node_id)

    def all(self) -> list[NodeSpec]:
        return [self._specs[k] for k in sorted(self._specs)]

    def catalog(self) -> list[dict[str, Any]]:
        return [spec.describe() for spec in self.all()]

    def __len__(self) -> int:
        return len(self._specs)
