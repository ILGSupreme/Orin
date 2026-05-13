from __future__ import annotations

from typing import Any

from cortex.cluster.discovery.discovery_k8s import KubernetesDiscoveryProvider
from cortex.cluster.discovery.prober import BackendHealthProber
from cortex.cluster.discovery.registry import (
    InMemoryBackendRegistry,
    RegistryRefresher,
)
from cortex.cluster.discovery.selector import BackendSelectionRequest, BackendSelector


class DiscoveryService:
    def __init__(
        self,
        refresh_interval_seconds: float = 15.0,
        health_timeout: float = 2.0,
    ) -> None:
        self.discovery = KubernetesDiscoveryProvider()
        self.prober = BackendHealthProber(timeout=health_timeout)
        self.registry = InMemoryBackendRegistry(
            discovery=self.discovery,
            prober=self.prober,
        )
        self.refresher = RegistryRefresher(
            registry=self.registry,
            refresh_interval_seconds=refresh_interval_seconds,
        )
        self.selector = BackendSelector(self.registry)

    def get_registry(self):
        return self.registry

    def get_capability_summary(self) -> dict[str, Any]:

        backends = self.registry.list_backends()

        roles: set[str] = set()
        capabilities_by_role: dict[str, set[str]] = {}
        modalities_by_role: dict[str, set[str]] = {}

        cortex_available = False

        for b in backends:
            role = getattr(b, "role", None)
            if not role:
                continue

            roles.add(role)

            capabilities_by_role.setdefault(role, set())
            modalities_by_role.setdefault(role, set())

            for cap in getattr(b, "capabilities", []) or []:
                capabilities_by_role[role].add(str(cap))

            for mod in getattr(b, "modalities", []) or []:
                modalities_by_role[role].add(str(mod))

            if role == "cortex":
                cortex_available = True

        return {
            "roles": sorted(roles),
            "capabilities_by_role": {
                role: sorted(values) for role, values in capabilities_by_role.items()
            },
            "modalities_by_role": {
                role: sorted(values) for role, values in modalities_by_role.items()
            },
            "deferred_available": cortex_available,
        }

    def find_candidates(
        self,
        *,
        role: str | None = None,
        required_capabilities: list[str] | None = None,
        required_modalities: list[str] | None = None,
    ):
        return self.selector.select(
            BackendSelectionRequest(
                role=role,
                required_capabilities=required_capabilities,
                required_modalities=required_modalities,
                healthy_only=True,
            )
        )
