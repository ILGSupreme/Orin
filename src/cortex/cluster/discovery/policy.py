from typing import Any

from cortex.cluster.discovery.selector import BackendSelectionRequest, BackendSelector
from cortex.cluster.discovery.types import BackendDescriptor


class BackendRoutingPolicy:
    def __init__(self, selector: BackendSelector) -> None:
        self.selector = selector

    def select_backend(
        self,
        *,
        role: str | None = None,
        required_capabilities: list[str] | None = None,
        required_modalities: list[str] | None = None,
        runtime_preference: list[dict[str, Any]] | None = None,
        allow_degraded_fallback: bool = True,
    ) -> BackendDescriptor | None:
        primary = self.selector.pick_one(
            BackendSelectionRequest(
                role=role,
                required_capabilities=required_capabilities,
                required_modalities=required_modalities,
                runtime_preference=runtime_preference,
                healthy_only=True,
            )
        )
        if primary is not None:
            return primary

        if not allow_degraded_fallback:
            return None

        matches = self.selector.select(
            BackendSelectionRequest(
                role=role,
                required_capabilities=required_capabilities,
                required_modalities=required_modalities,
                runtime_preference=runtime_preference,
                healthy_only=False,
            )
        )

        for backend in matches:
            if backend.health.status.status == "degraded":
                return backend

        return None

    def get_all_candidates(
        self,
        *,
        role: str,
        required_capabilities: list[str] | None = None,
        required_modalities: list[str] | None = None,
    ) -> list[BackendDescriptor]:
        return self.selector.select(
            BackendSelectionRequest(
                role=role,
                required_capabilities=required_capabilities,
                required_modalities=required_modalities,
            )
        )
