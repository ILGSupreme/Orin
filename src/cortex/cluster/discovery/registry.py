from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from cortex.cluster.discovery.discovery_k8s import KubernetesDiscoveryProvider
from cortex.cluster.discovery.prober import BackendHealthProber
from cortex.cluster.discovery.types import (
    BackendDescriptor,
)

root_logger = logging.getLogger()

class BackendRegistry(ABC):
    @abstractmethod
    async def refresh(self) -> list[BackendDescriptor]:
        raise NotImplementedError

    @abstractmethod
    def list_backends(self) -> list[BackendDescriptor]:
        raise NotImplementedError

    @abstractmethod
    def find_by_role(
        self, role: str, *, healthy_only: bool = True
    ) -> list[BackendDescriptor]:
        raise NotImplementedError


class InMemoryBackendRegistry(BackendRegistry):
    def __init__(
        self,
        discovery: KubernetesDiscoveryProvider,
        prober: BackendHealthProber | None = None,
    ) -> None:
        self.discovery = discovery
        self.prober = prober
        self._backends: list[BackendDescriptor] = []
        self._lock = asyncio.Lock()

    async def refresh(self) -> list[BackendDescriptor]:
        async with self._lock:
            backends = await self.discovery.discover()

            if self.prober is not None:
                backends = await self.prober.probe_all(backends)

            root_logger.info(f"refresh: {backends}")

            self._backends = sorted(
                backends,
                key=lambda b: (
                    0 if b.is_healthy else 1,
                    -b.priority,
                    -b.weight,
                    b.name,
                ),
            )

            root_logger.info("Backend registry refreshed: %d backends", len(self._backends))
            return list(self._backends)

    def list_backends(self) -> list[BackendDescriptor]:
        return list(self._backends)

    def find_by_role(
        self, role: str, *, healthy_only: bool = True
    ) -> list[BackendDescriptor]:
        items = [b for b in self._backends if b.role == role]
        if healthy_only:
            items = [b for b in items if b.is_healthy]
        return items


class RegistryRefresher:
    def __init__(
        self,
        registry: InMemoryBackendRegistry,
        refresh_interval_seconds: float = 15.0,
    ) -> None:
        self.registry = registry
        self.refresh_interval_seconds = refresh_interval_seconds
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        await self.registry.refresh()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.sleep(self.refresh_interval_seconds)
                await self.registry.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                root_logger.exception("Backend registry refresh failed")
