import logging
from dataclasses import dataclass
from typing import Any

from cortex.cluster.discovery.registry import BackendRegistry
from cortex.cluster.discovery.types import BackendDescriptor

root_logger = logging.getLogger()

@dataclass(slots=True)
class BackendSelectionRequest:
    role: str | None = None
    required_capabilities: list[str] | None = None
    required_modalities: list[str] | None = None
    runtime_preference: list[dict[str, Any]] | None = None
    healthy_only: bool = True


class BackendSelector:
    def __init__(self, registry: BackendRegistry) -> None:
        self.registry = registry

    def select(self, req: BackendSelectionRequest) -> list[BackendDescriptor]:
        backends = self.registry.list_backends()

        root_logger.debug(f"All available backends {backends}")

        if req.healthy_only:
            backends = [b for b in backends if b.is_healthy]

        if req.runtime_preference:
            root_logger.debug(f"runtime_preference {req.runtime_preference}")

            backends = [
                b
                for b in backends
                if b.runtime.preference_is_valid(req.runtime_preference)
            ]

        if req.role:
            print(f" requirement role: {req.role}")
            backends = [b for b in backends if b.role == req.role]

        if req.required_capabilities:
            print(f" requirement cap: {req.required_capabilities}")
            needed = set(req.required_capabilities)
            backends = [b for b in backends if needed.issubset(set(b.capabilities))]

        if req.required_modalities:
            print(f" requirement mod: {req.required_modalities}")
            needed = set(req.required_modalities)
            backends = [b for b in backends if needed.issubset(set(b.modalities))]

        root_logger.debug(
            f"Selected backends for BackendSelectionRequest: {req}, Backends: {backends}"
        )

        return sorted(
            backends,
            key=lambda b: (
                0 if b.is_healthy else 1,
                -b.priority,
                -b.weight,
                b.name,
            ),
        )

    def pick_one(self, req: BackendSelectionRequest) -> BackendDescriptor | None:
        matches = self.select(req)
        return matches[0] if matches else None
