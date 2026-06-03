from __future__ import annotations

import asyncio
import logging
from typing import Any

from common.jobs import Job, JobManager, JobSpec
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
        job_manager: JobManager,
        planner: Planner,
    ) -> None:
        self.backend_services = backend_services
        self.backend_client = backend_client
        self.planner = planner
        self.job_manager = job_manager

    async def send_many(self, batch: list[WorkPacket]) -> str:
        jobs: list[Job] = []

        for packet in batch:
            jobs.append(
                self.job_manager.start(
                    spec=JobSpec(
                        kind="router.sendmany",
                        payload={"packet": packet},
                    ),
                    runner=lambda job: execute_send(job, self),
                )
            )

        batch_id = self.job_manager.create_batch(jobs=jobs)

        return batch_id

    async def retrieve_many(self, results: list[WorkResult]) -> str:
        jobs: list[Job] = []

        for result in results:
            if result.status in ("completed", "failed"):
                jobs.append(
                    self.job_manager.start(
                        spec=JobSpec(
                            kind="router.retrievemany.passthrough",
                            payload={"result": result.model_dump(mode="json")},
                        ),
                        runner=execute_passthrough_result,
                    )
                )
                continue

            jobs.append(
                self.job_manager.start(
                    spec=JobSpec(
                        kind="router.retrievemany",
                        payload={"result": result.model_dump(mode="json")},
                    ),
                    runner=lambda job: execute_retrieve_completed(job, self),
                )
            )

        return self.job_manager.create_batch(jobs=jobs)

    async def send(self, packet: WorkPacket) -> WorkResult:
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
                        },
                        "work_details": {
                            "work_type": packet.work_type,
                            "operation": packet.operation,
                        },
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
        self, work_id: str, work_type: WorkType, operation: str, backend: dict[str, Any]
    ) -> WorkResult:
        if backend is None:
            return WorkResult(
                status="failed",
                work_id=work_id,
                error="No suitable backend found",
            )

        try:
            rsp_result = await self.backend_client.retrieve_work(
                backend=backend, work_id=work_id
            )

            return WorkResult.model_validate(rsp_result)

        except Exception as exc:
            logging.exception("Router execution failed for work_id=%s", work_id)
            return WorkResult(
                status="failed",
                work_id=work_id,
                backend_name=getattr(backend, "name", None),
                backend_model=getattr(backend, "model", None),
                error=str(exc),
                metadata={
                    "work_type": work_type,
                    "operation": operation,
                },
            )

    async def retrieve_completed_work(
        self,
        work_id: str,
        work_type: WorkType,
        operation: str,
        backend: dict[str, Any],
        *,
        poll_interval_seconds: float = 1.0,
        timeout_seconds: float = 120.0,
    ) -> WorkResult:
        result = WorkResult(status="running", work_id=work_id)
        deadline = asyncio.get_running_loop().time() + timeout_seconds

        while result.status in ("accepted", "running"):
            if asyncio.get_running_loop().time() >= deadline:
                return WorkResult(
                    status="failed",
                    work_id=work_id,
                    error=f"Timed out waiting for work to complete after {timeout_seconds:.1f}s",
                    metadata={
                        "backend_ref": backend,
                        "work_type": work_type,
                        "operation": operation,
                        "timeout_seconds": timeout_seconds,
                    },
                )

            await asyncio.sleep(poll_interval_seconds)

            result = await self.retrieve_work(
                work_id=work_id,
                work_type=work_type,
                operation=operation,
                backend=backend,
            )

        return result

    def _resolve_backend(self, packet: WorkPacket):
        backend_name = packet.metadata.get("backend_name")
        if backend_name:
            for backend in self.backend_services.get_registry().list_backends():
                if getattr(backend, "name", None) == backend_name:
                    return backend

        return self.planner.select_backend(packet)

async def execute_passthrough_result(job: Job) -> WorkResult:
    return WorkResult.model_validate(job.spec.payload["result"])

async def execute_send(job: Job, router: RouterService) -> WorkResult:
    packet = WorkPacket.model_validate(job.spec.payload.get("packet"))
    return await router.send(packet=packet)


async def execute_retrieve_completed(job: Job, router: RouterService) -> WorkResult:
    result = WorkResult.model_validate(job.spec.payload.get("result"))

    if result.status in ("completed", "failed"):
        return result

    work_detail = result.metadata.get("work_details")
    backend = result.metadata.get("backend_ref")

    if not work_detail:
        raise ValueError("Work detail is empty in result")

    if not backend:
        raise ValueError("backend is empty")

    work_type = (
        work_detail.get("work_type", "")
        if isinstance(work_detail, dict)
        else getattr(work_detail, "work_type")
    )

    operation = (
        work_detail.get("operation", "")
        if isinstance(work_detail, dict)
        else getattr(work_detail, "operation")
    )

    return await router.retrieve_completed_work(
        work_id=result.work_id,
        work_type=work_type,
        operation=operation,
        backend=backend,
    )
