from __future__ import annotations

import logging
from typing import Any

from common.protocol.routing_types import (
    WorkPacket,
    WorkResult,
    WorkType,
)
from cortex.cluster.discovery.client import BackendClient
from cortex.cluster.discovery.services import DiscoveryService
from cortex.router.planner import Planner


class RouterService:
    def __init__(
        self,
        *,
        backend_services: DiscoveryService,
        backend_client: BackendClient,
        planner: Planner,
    ) -> None:
        self.backend_services = backend_services
        self.backend_client = backend_client
        self.planner = planner
        self.cortex_mailbox = None

    def set_cortex_mailbox(self, mailbox) -> None:
        self.cortex_mailbox = mailbox

    async def execute(self, packet: WorkPacket) -> WorkResult:
        if packet.work_type == WorkType.CORTEX:
            if self.cortex_mailbox is None:
                return WorkResult(
                    status="failed",
                    work_id=packet.work_id,
                    error="Cortex mailbox is not configured",
                )
            return await self.cortex_mailbox.receive(packet)

        backend = self._resolve_backend(packet)
        if backend is None:
            return WorkResult(
                status="failed",
                work_id=packet.work_id,
                error="No suitable backend found",
            )

        try:
            rsp = await self.backend_client.submit_packet(
                backend=backend,
                packet=packet,
            )
            logging.info(f"response: {rsp}")

            if rsp.get("status") in ("completed", "failed"):
                return WorkResult.model_validate(rsp)

            if rsp.get("status") == "accepted":
                result = WorkResult.model_validate(rsp)
                result.backend_name = getattr(backend, "name", None)
                result.backend_model = getattr(backend, "model", None)
                result.metadata.update(
                    {
                        "backend_ref": {
                            "name": backend.name,
                            "namespace": backend.namespace,
                            "service_name": backend.service_name,
                            "url": backend.url,
                            "work_path": backend.work_path,
                        }
                    }
                )
                return result

            raise ValueError("Faulty response")

        except Exception as exc:
            logging.exception("Router execution failed for work_id=%s", packet.work_id)
            return WorkResult(
                status="failed",
                work_id=packet.work_id,
                backend_name=getattr(backend, "name", None),
                backend_model=getattr(backend, "model", None),
                error=str(exc),
                metadata={
                    "work_type": packet.work_type,
                    "operation": packet.operation,
                },
            )

    async def retrieve_work(
        self, packet: WorkPacket, backend: dict[str, Any]
    ) -> WorkResult:
        if backend is None:
            return WorkResult(
                status="failed",
                work_id=packet.work_id,
                error="No suitable backend found",
            )

        try:
            rsp_result = await self.backend_client.retrieve_work(
                backend=backend, work_id=packet.work_id
            )

            return WorkResult.model_validate(rsp_result)

        except Exception as exc:
            logging.exception("Router execution failed for work_id=%s", packet.work_id)
            return WorkResult(
                status="failed",
                work_id=packet.work_id,
                backend_name=getattr(backend, "name", None),
                backend_model=getattr(backend, "model", None),
                error=str(exc),
                metadata={
                    "work_type": packet.work_type,
                    "operation": packet.operation,
                },
            )

    def _resolve_backend(self, packet: WorkPacket):
        backend_name = packet.metadata.get("backend_name")
        if backend_name:
            for backend in self.backend_services.get_registry().list_backends():
                if getattr(backend, "name", None) == backend_name:
                    return backend

        return self.planner.select_backend(packet)
