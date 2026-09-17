from __future__ import annotations

import logging
from typing import Any

from common.jobs import Job, JobManager, JobSpec
from common.primer import Primer
from common.protocol.ingress_types import LoadModelRequest
from common.protocol.routing_types import (
    WorkPacket,
    WorkResult,
)

root_logger = logging.getLogger()

class GenerativeModelRuntime:
    def __init__(self, *, primer: Primer, job_manager: JobManager) -> None:
        self.primer = primer
        self.job_manager = job_manager
        self.work_responses = {}

    async def handle_ingress(self, req: WorkPacket) -> WorkResult:
        spec = JobSpec(
            kind="llm.chat",
            payload={
                "packet": req,
            },
            job_id=req.work_id,
        )

        job = self.job_manager.start(
            spec=spec,
            runner=self._run_workpacket_job,
        )

        return WorkResult(
            status="accepted",
            work_id=job.job_id,
        )

    async def handle_egress(self, work_id: str) -> WorkResult:
        job = self.job_manager.get(work_id)

        if job is None:
            return WorkResult(
                status="failed",
                work_id=work_id,
                error=f"Unknown work_id: {work_id}",
            )

        if job.status in {"accepted", "running"}:
            return WorkResult(
                status=job.status,
                work_id=job.job_id,
            )

        if job.status == "failed":
            return WorkResult(
                status="failed",
                work_id=job.job_id,
                error=job.error,
            )

        result = job.get_result()

        if isinstance(result, WorkResult):
            return result

        if isinstance(result, dict):
            return WorkResult(
                status=job.status,
                work_id=job.job_id,
                content=result.get("content", []),
                backend_model=result.get("backend_model"),
                error=result.get("error"),
                metadata=result.get("metadata", {}),
            )

        return WorkResult(
            status="completed",
            work_id=job.job_id,
            metadata={"result": result},
        )
        
    async def _run_workpacket_job(self, job: Job) -> WorkResult:
        packet = WorkPacket.model_validate(job.spec.payload["packet"])
        task = packet.task

        messages = await self.primer.chat_text_message(
            messages=task.messages,
            constraints=task.constraints,
            operation=task.operation,
        )

        return WorkResult(
            status="completed",
            work_id=packet.work_id,
            content=messages,
            backend_model=self.primer.status().get("model_id"),
            metadata={
                "engine": self.primer.engine_type,
            },
        )
        
    async def run_load_model_job(self, job: Job) -> dict[str, Any]:
        req = LoadModelRequest.model_validate(job.spec.payload)

        model_id = req.model_id
        provider = req.provider
        engine = req.engine
        repo_id = req.repo_id
        filename = req.filename
        revision = req.revision
        tokenizer_id = req.tokenizer_id
        force_reload = req.force_reload

        if not engine:
            raise ValueError("No engine selected for model load job")

        current_engine = self.primer.status().get("engine")

        if current_engine != engine:
            root_logger.info(
                "Switching engine: %s -> %s",
                current_engine,
                engine,
            )
            await self.primer.load_engine(engine)

        active_engine = self.primer.status().get("engine")

        if active_engine != engine:
            raise RuntimeError(
                f"Engine switch failed: requested={engine}, active={active_engine}"
            )

        if active_engine == "gguf":
            if not repo_id or not filename:
                raise ValueError("GGUF engine requires repo_id and filename")

            root_logger.info(
                "Loading GGUF model: model=%s repo=%s file=%s revision=%s",
                model_id,
                repo_id,
                filename,
                revision,
            )

            await self.primer.load_model(
                provider=provider,
                model_id=model_id,
                repo_id=repo_id,
                filename=filename,
                revision=revision,
                tokenizer_id=tokenizer_id,
                force_reload=force_reload,
            )

        else:
            root_logger.info(
                "Loading model: model=%s engine=%s provider=%s",
                model_id,
                active_engine,
                provider,
            )

            await self.primer.load_model(
                model_id=model_id,
                provider=provider,
                force_reload=force_reload,
            )

        return {
            "model_id": model_id,
            "engine": active_engine,
            "primer": self.primer.status(),
        }
