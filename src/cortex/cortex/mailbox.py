from __future__ import annotations

import asyncio
import logging
from typing import Any

from common.protocol.routing_types import (
    CanonicalTask,
    WorkDisposition,
    WorkOrigin,
    WorkPacket,
    WorkResult,
    WorkType,
)
from cortex.router.service import RouterService


class CortexMailbox:
    def __init__(self, router: RouterService) -> None:
        self.router = router
        self._tasks: dict[str, asyncio.Task] = {}
        self._task_state: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def submit(self, packet: WorkPacket) -> WorkResult:
        return await self.router.execute(packet)

    async def retrieve(self, packet: WorkPacket, backend: dict[str, Any]) -> WorkResult:
        return await self.router.retrieve_work(packet=packet, backend=backend)

    async def receive(self, packet: WorkPacket) -> WorkResult:
        if packet.work_type != WorkType.CORTEX:
            return WorkResult(
                status="failed",
                work_id=packet.work_id,
                error="Cortex mailbox only accepts cortex work packets",
            )

        if packet.disposition != WorkDisposition.DEFERRED:
            return WorkResult(
                status="failed",
                work_id=packet.work_id,
                error="Cortex mailbox only accepts deferred cortex work",
            )

        if packet.origin_stage != WorkOrigin.MAIN_INGRESS:
            return WorkResult(
                status="failed",
                work_id=packet.work_id,
                error="Deferred cortex work may only originate from main_ingress",
            )

        return await self._submit_deferred(packet)

    async def _submit_deferred(self, packet: WorkPacket) -> WorkResult:
        async with self._lock:
            if packet.work_id in self._tasks:
                return WorkResult(
                    status="running",
                    work_id=packet.work_id,
                    metadata={"message": "Work already running"},
                )

            self._task_state[packet.work_id] = {
                "status": "running",
                "result": None,
                "error": None,
            }
            self._tasks[packet.work_id] = asyncio.create_task(
                self._run_deferred(packet)
            )

        return WorkResult(
            status="accepted",
            work_id=packet.work_id,
            metadata={"message": "Deferred cortex work accepted"},
        )

    async def _run_deferred(self, packet: WorkPacket) -> None:
        try:
            result = await self._handle_cortex_task(packet)
            self._task_state[packet.work_id] = {
                "status": result.status,
                "result": result.model_dump(),
                "error": result.error,
            }
        except Exception as exc:
            logging.exception(
                "Deferred cortex execution failed for work_id=%s",
                packet.work_id,
            )
            self._task_state[packet.work_id] = {
                "status": "failed",
                "result": None,
                "error": str(exc),
            }
        finally:
            async with self._lock:
                self._tasks.pop(packet.work_id, None)

    async def _handle_cortex_task(self, packet: WorkPacket) -> WorkResult:
        if packet.operation != "background_followup":
            return WorkResult(
                status="failed",
                work_id=packet.work_id,
                error=f"Unsupported cortex operation: {packet.operation}",
            )

        next_task_raw = packet.task.inputs.get("next_task")
        if not isinstance(next_task_raw, dict):
            return WorkResult(
                status="failed",
                work_id=packet.work_id,
                error="Missing or invalid next_task for background_followup",
            )

        child_packet = WorkPacket(
            work_id=f"{packet.work_id}:child",
            disposition=WorkDisposition.DIRECT,
            task=CanonicalTask(**next_task_raw),
            metadata={
                **packet.metadata,
                "origin_stage": "local_cortex_worker",
                "parent_work_id": packet.work_id,
            },
        )
        return await self.router.execute(child_packet)

    def get_work_state(self, work_id: str) -> dict[str, Any] | None:
        return self._task_state.get(work_id)
