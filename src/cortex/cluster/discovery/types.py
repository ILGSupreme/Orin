from __future__ import annotations

import dataclasses
from dataclasses import asdict, field
from typing import Any, Literal

BackendRole = Literal["cortex", "llm", "tool", "memory"]

HealthStatus = Literal["healthy", "degraded", "unavailable", "unhealthy", "unreachable"]


VALIDATORS = {
    int: lambda current, req: current >= req,
    str: lambda current, req: current == req,
    bool: lambda current, req: current == req,
}


@dataclasses.dataclass(slots=True)
class BackendHealthStatus:
    status: HealthStatus = "unavailable"
    error: str = ""


@dataclasses.dataclass(slots=True)
class BackendHealth:
    registered: bool = False
    ready_endpoints: int = 0
    http_healthy: bool = False
    model_healthy: bool = False
    status: BackendHealthStatus = field(default_factory=(BackendHealthStatus))


@dataclasses.dataclass(slots=True)
class RuntimeMetaData:
    effective_n_ctx: int = 0

    def preference_is_valid(self, preference: list[dict[str, Any]]) -> bool:
        items = [key for d in preference for key in d.items()]

        for key, value in items:
            if not hasattr(self, key):
                return False

            current_value = getattr(self, key)
            validator = VALIDATORS.get(type(current_value), None)
            if validator is None:
                return False
            if validator and not validator(current_value, value):
                return False
        return True

    def has_keys(self, preference: list[dict[str, Any]]) -> bool:
        # Check if every key in every dict exists in this object
        return all(hasattr(self, k) for d in preference for k in d)


@dataclasses.dataclass(slots=True)
class BackendDescriptor:
    name: str
    namespace: str
    service_name: str
    url: str

    role: BackendRole | str

    kind: str | None = None
    model: str | None = None

    capabilities: list[str] = field(default_factory=list)
    modalities: list[str] = field(default_factory=list)

    priority: int = 100
    weight: float = 1.0
    visibility: str = "internal"

    health_path: str = "/health"
    work_path: str = "/"
    model_status_path: str | None = None

    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)

    health: BackendHealth = field(default_factory=BackendHealth)
    runtime: RuntimeMetaData = field(default_factory=RuntimeMetaData)

    @property
    def is_healthy(self) -> bool:
        return self.health.status.status == "healthy"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)