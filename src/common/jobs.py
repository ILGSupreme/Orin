from __future__ import annotations

import asyncio
import inspect
import logging
import time as monotonic_time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal
from common.presentation import formatting
from common import time
from common.protocol.routing_types import WorkResult

JobStatus = Literal["accepted", "running", "completed", "failed"]
StageStatus = Literal["accepted", "running", "completed", "failed"]

JobRunner = Callable[["Job"], Awaitable[Any] | Any]
JobCompletedHook = Callable[["Job"], Awaitable[None] | None]

PipelineStageRunner = Callable[["Job"], Awaitable[WorkResult]]
PipelineStagePoller = Callable[["Job", WorkResult], Awaitable[WorkResult]]


@dataclass(slots=True)
class JobSpec:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    job_id: str | None = None


@dataclass(slots=True)
class PipelineStage:
    name: str
    runner: PipelineStageRunner
    poller: PipelineStagePoller | None = None
    timeout_seconds: float = 120.0
    poll_interval_seconds: float = 1.0


@dataclass(slots=True)
class PipelineDefinition:
    name: str
    stages: list[PipelineStage]


@dataclass(slots=True)
class Job:
    job_id: str
    spec: JobSpec

    status: JobStatus = "accepted"
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)

    pipeline_name: str | None = None
    current_stage: str | None = None
    stage_index: int = 0
    stage_count: int = 0
    stage_status: StageStatus | None = None
    stage_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    latest_result: dict[str, Any] = field(default_factory=dict)
    progress_message: str | None = None

    created_at: str = field(default_factory=time.utcnow_iso)
    updated_at: str = field(default_factory=time.utcnow_iso)

    def update_status(self, status: JobStatus) -> None:
        self.status = status
        self.updated_at = time.utcnow_iso()

    def update_stage(
        self,
        *,
        name: str,
        index: int,
        count: int,
        status: StageStatus,
        message: str | None = None,
    ) -> None:
        self.current_stage = name
        self.stage_index = index
        self.stage_count = count
        self.stage_status = status
        self.progress_message = message
        self.updated_at = time.utcnow_iso()

    def progress_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.spec.kind,
            "status": self.status,
            "error": self.error,
            "pipeline_name": self.pipeline_name,
            "current_stage": self.current_stage,
            "stage_status": self.stage_status,
            "stage_index": self.stage_index,
            "stage_count": self.stage_count,
            "progress_message": self.progress_message,
            "latest_result": self.latest_result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.status != "failed",
            "job_id": self.job_id,
            "kind": self.spec.kind,
            "status": self.status,
            "error": self.error,
            "payload": self.spec.payload,
            "result": self.result,
            "pipeline_name": self.pipeline_name,
            "progress": self.progress_dict(),
            "stage_results": self.stage_results,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

@dataclass(slots=True)
class BatchWork():
    job_ids: list[str] = field(default_factory=list)
    batch_id: str = ""

class JobManager:
    def __init__(
        self,
        *,
        max_jobs: int = 256,
        max_concurrent_jobs: int = 4,
    ) -> None:
        self.jobs: dict[str, Job] = {}
        self.pipelines: dict[str, PipelineDefinition] = {}
        self.max_jobs = max_jobs
        self.batch : dict[str, BatchWork] = {}
        self.job_semaphore = asyncio.Semaphore(max_concurrent_jobs)
        self.pipeline_semaphore = asyncio.Semaphore(max_concurrent_jobs)

    # -------------------------------------------------------------------------
    # Job inspection
    # -------------------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def get_progress(self, job_id: str) -> dict[str, Any] | None:
        job = self.get(job_id)
        if not job:
            return None
        return job.progress_dict()

    def list_jobs(self) -> list[Job]:
        return list(self.jobs.values())

    def list_active_jobs(self) -> list[Job]:
        return [
            job
            for job in self.jobs.values()
            if job.status in ("accepted", "running")
        ]

    def list_jobs_for_session(self, session_id: str) -> list[Job]:
        return [
            job
            for job in self.jobs.values()
            if job.spec.payload.get("session_id") == session_id
            or job.spec.payload.get("inference_object", {}).get("session_id") == session_id
        ]

    def list_active_jobs_for_session(self, session_id: str) -> list[Job]:
        return [
            job
            for job in self.list_jobs_for_session(session_id)
            if job.status in ("accepted", "running")
        ]

    def summarize_jobs_for_prompt(self, session_id: str | None = None) -> str:
        jobs = (
            self.list_jobs_for_session(session_id)
            if session_id
            else self.list_jobs()
        )

        active = [
            job for job in jobs
            if job.status in ("accepted", "running")
        ]

        recent_done = [
            job for job in jobs
            if job.status in ("completed", "failed")
        ][-5:]

        lines: list[str] = []

        if active:
            lines.append("Active background jobs:")
            for job in active:
                stage = (
                    f"{job.stage_index}/{job.stage_count} {job.current_stage}"
                    if job.current_stage
                    else "no stage"
                )

                lines.append(
                    f"- {job.job_id}: status={job.status}, "
                    f"stage={stage}, kind={job.spec.kind}"
                )

        if recent_done:
            lines.append("Recent background jobs:")
            for job in recent_done:
                if job.status == "failed":
                    lines.append(
                        f"- {job.job_id}: status=failed, "
                        f"kind={job.spec.kind}, "
                        f"error={job.error or 'unknown error'}"
                    )
                else:
                    lines.append(
                        f"- {job.job_id}: status=completed, "
                        f"kind={job.spec.kind}, "
                        f"result={job.spec.payload}"
                    )

        if not lines:
            return "No active or recent background jobs."

        return "\n".join(lines)
    
    def create_batch(self, jobs: list[Job]) -> str:
        batch_id = uuid.uuid4().hex[:8]

        self.batch[batch_id] = BatchWork(
            job_ids=[job.job_id for job in jobs],
            batch_id=batch_id,
        )

        return batch_id
    
    async def batch_progress(
        self,
        batch_id: str,
        *,
        blocking: bool = True,
        poll_interval_seconds: float = 1.0,
        timeout_seconds: float = 300.0,
    ) -> list[dict[str,Any]]:
        

        batch_work = self.batch.get(batch_id)
        logging.info(f" batch work : {batch_work}")

        if not batch_work:
            raise ValueError(f"No batch found for id: {batch_id}")

        if not blocking:
            return self.batch_check(batch_work=batch_work)

        deadline = asyncio.get_running_loop().time() + timeout_seconds

        while True:
            logging.info(f" {batch_work} ")
            results = self.batch_check(batch_work=batch_work)
            logging.info(f" {results} ")

            if len(results) == len(batch_work.job_ids):
                return results

            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Batch timed out: {batch_id}")

            await asyncio.sleep(poll_interval_seconds)
        
    def batch_check(self, batch_work: BatchWork) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        for job_id in batch_work.job_ids:
            job = self.get(job_id=job_id)

            if not job:
                raise ValueError(f"Job id in batch not found: {job_id}")

            if job.status in ("completed", "failed"):
                results.append(job.result)

        return results

    # -------------------------------------------------------------------------
    # Pipeline definition
    # -------------------------------------------------------------------------

    def define_pipeline(
        self,
        name: str,
        *,
        stages: list[PipelineStage],
        replace: bool = False,
    ) -> None:
        if not stages:
            raise ValueError("Pipeline must contain at least one stage")

        if name in self.pipelines and not replace:
            raise ValueError(f"Pipeline already defined: {name}")

        self.pipelines[name] = PipelineDefinition(
            name=name,
            stages=stages,
        )

    def get_pipeline(self, name: str) -> PipelineDefinition:
        pipeline = self.pipelines.get(name)
        if not pipeline:
            raise ValueError(f"Unknown pipeline: {name}")
        return pipeline

    # -------------------------------------------------------------------------
    # Start jobs
    # -------------------------------------------------------------------------

    def _create_job(self, spec: JobSpec) -> Job:
        if len(self.jobs) >= self.max_jobs:
            raise RuntimeError(f"Job limit reached: {self.max_jobs}")

        job_id = spec.job_id or f"{spec.kind}:{uuid.uuid4().hex[:8]}"

        if job_id in self.jobs:
            raise RuntimeError(f"Job already exists: {job_id}")

        job = Job(
            job_id=job_id,
            spec=spec,
        )

        self.jobs[job_id] = job
        return job

    def start(
        self,
        *,
        spec: JobSpec,
        runner: JobRunner,
        on_completed: JobCompletedHook | None = None,
    ) -> Job:
        job = self._create_job(spec)

        asyncio.create_task(
            self._run_job(
                job_id=job.job_id,
                runner=runner,
                on_completed=on_completed,
            )
        )

        return job

    def start_pipeline(
        self,
        *,
        spec: JobSpec,
        pipeline: str,
        on_completed: JobCompletedHook | None = None,
    ) -> Job:
        definition = self.get_pipeline(pipeline)

        job = self._create_job(spec)
        job.pipeline_name = pipeline
        job.stage_count = len(definition.stages)

        asyncio.create_task(
            self._run_pipeline(
                job_id=job.job_id,
                definition=definition,
                on_completed=on_completed,
            )
        )

        return job

    # -------------------------------------------------------------------------
    # Single job runner
    # -------------------------------------------------------------------------

    async def _run_job(
        self,
        *,
        job_id: str,
        runner: JobRunner,
        on_completed: JobCompletedHook | None,
    ) -> None:
        job = self.jobs[job_id]

        async with self.job_semaphore:
            try:
                job.update_status("running")
                job.update_stage(
                    name="runner",
                    index=1,
                    count=1,
                    status="running",
                    message="Running job",
                )

                result = runner(job)
                if inspect.isawaitable(result):
                    result = await result

                normalized =formatting.as_dict(result)
                job.latest_result = normalized
                job.result = normalized

                if isinstance(result, WorkResult):
                    if result.status == "failed":
                        job.error = result.error
                        job.update_stage(
                            name="runner",
                            index=1,
                            count=1,
                            status="failed",
                            message=result.error,
                        )
                        job.update_status("failed")
                        return

                if on_completed is not None:
                    hook_result = on_completed(job)
                    if inspect.isawaitable(hook_result):
                        await hook_result

                job.update_stage(
                    name="runner",
                    index=1,
                    count=1,
                    status="completed",
                    message="Job completed",
                )
                job.update_status("completed")

                logging.info(
                    "Job completed: job_id=%s kind=%s",
                    job.job_id,
                    job.spec.kind,
                )

            except Exception as exc:
                job.update_status("failed")
                job.stage_status = "failed"
                job.error = str(exc)

                logging.error(
                    "Job failed: job_id=%s kind=%s error=%s\n%s",
                    job.job_id,
                    job.spec.kind,
                    exc,
                    traceback.format_exc(),
                )

    # -------------------------------------------------------------------------
    # Pipeline runner
    # -------------------------------------------------------------------------

    async def _run_pipeline(
        self,
        *,
        job_id: str,
        definition: PipelineDefinition,
        on_completed: JobCompletedHook | None,
    ) -> None:
        job = self.jobs[job_id]

        async with self.pipeline_semaphore:
            try:
                job.update_status("running")

                latest_result: WorkResult | None = None

                for index, stage in enumerate(definition.stages, start=1):
                    job.update_stage(
                        name=stage.name,
                        index=index,
                        count=len(definition.stages),
                        status="running",
                        message=f"Running stage {index}/{len(definition.stages)}: {stage.name}",
                    )

                    result : WorkResult = await stage.runner(job)

                    result = await self._wait_for_stage_result(
                        job=job,
                        stage=stage,
                        index=index,
                        count=len(definition.stages),
                        result=result,
                    )

                    normalized = formatting.as_dict(result)
                    latest_result = result

                    job.stage_results[stage.name] = normalized
                    job.latest_result = normalized

                    self._merge_stage_result_into_payload(
                        job=job,
                        stage_name=stage.name,
                        result=result,
                        normalized=normalized,
                    )

                    if result.status == "failed":
                        job.error = result.error or f"Pipeline stage failed: {stage.name}"
                        job.update_stage(
                            name=stage.name,
                            index=index,
                            count=len(definition.stages),
                            status="failed",
                            message=job.error,
                        )
                        job.update_status("failed")
                        return

                    job.update_stage(
                        name=stage.name,
                        index=index,
                        count=len(definition.stages),
                        status="completed",
                        message=f"Completed stage {index}/{len(definition.stages)}: {stage.name}",
                    )

                job.result = {
                    "status": "completed",
                    "latest_result": formatting.as_dict(latest_result),
                    "stage_results": job.stage_results,
                }

                if on_completed is not None:
                    hook_result = on_completed(job)
                    if inspect.isawaitable(hook_result):
                        await hook_result

                job.update_status("completed")

                logging.info(
                    "Pipeline completed: job_id=%s kind=%s pipeline=%s",
                    job.job_id,
                    job.spec.kind,
                    definition.name,
                )

            except Exception as exc:
                job.update_status("failed")
                job.stage_status = "failed"
                job.error = str(exc)

                logging.error(
                    "Pipeline failed: job_id=%s kind=%s pipeline=%s stage=%s error=%s\n%s",
                    job.job_id,
                    job.spec.kind,
                    definition.name,
                    job.current_stage,
                    exc,
                    traceback.format_exc(),
                )

    async def _wait_for_stage_result(
        self,
        *,
        job: Job,
        stage: PipelineStage,
        index: int,
        count: int,
        result: WorkResult,
    ) -> WorkResult:
        
        logging.info(f"Wait for stage: result is :{result}")
        if result.status in ("completed", "failed"):
            return result

        if result.status not in ("accepted", "running"):
            return WorkResult(
                status="failed",
                work_id=result.work_id,
                error=f"Unsupported WorkResult status from stage {stage.name}: {result.status}",
                metadata={
                    "stage": stage.name,
                    "original_status": result.status,
                },
            )

        if stage.poller is None:
            return WorkResult(
                status="failed",
                work_id=result.work_id,
                error=(
                    f"Stage {stage.name} returned {result.status}, "
                    "but no poller was configured."
                ),
                metadata={
                    "stage": stage.name,
                    "original_result": result.model_dump(),
                },
            )

        deadline = monotonic_time.monotonic() + stage.timeout_seconds
        latest = result

        while latest.status in ("accepted", "running"):
            logging.info(f"Polling stage: result is :{latest}")
            if monotonic_time.monotonic() >= deadline:
                return WorkResult(
                    status="failed",
                    work_id=latest.work_id,
                    error=f"Stage {stage.name} timed out after {stage.timeout_seconds} seconds.",
                    metadata={
                        "stage": stage.name,
                        "last_result": latest.model_dump(),
                    },
                )

            job.update_stage(
                name=stage.name,
                index=index,
                count=count,
                status="running",
                message=(
                    f"Waiting for stage {index}/{count}: {stage.name}; "
                    f"latest_status={latest.status}"
                ),
            )

            await asyncio.sleep(stage.poll_interval_seconds)

            polled: WorkResult = await stage.poller(job, latest)

            latest = polled
            job.latest_result = formatting.as_dict(latest)

        return latest

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _merge_stage_result_into_payload(
        self,
        *,
        job: Job,
        stage_name: str,
        result: WorkResult,
        normalized: dict[str, Any],
    ) -> None:
        job.spec.payload["latest_stage"] = stage_name
        job.spec.payload["latest_result"] = normalized
        job.spec.payload["response"] = normalized
        job.spec.payload["stage_results"] = job.stage_results

        if result.metadata:
            job.spec.payload.update(result.metadata)