from __future__ import annotations

import asyncio
import logging
import traceback
import uuid
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from common import time

JobStatus = Literal["accepted", "running", "completed", "failed"]

JobRunner = Callable[["Job"], Awaitable[Any] | Any]
JobCompletedHook = Callable[["Job"], Awaitable[None] | None]


@dataclass(slots=True)
class JobSpec:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    job_id: str | None = None


@dataclass(slots=True)
class Job:
    job_id: str
    spec: JobSpec
    status: JobStatus = "accepted"
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=time.utcnow_iso)
    updated_at: str = field(default_factory=time.utcnow_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.status != "failed",
            "job_id": self.job_id,
            "kind": self.spec.kind,
            "status": self.status,
            "error": self.error,
            "payload": self.spec.payload,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
    
    def update_status(self, status:JobStatus):
        self.status = status
        self.updated_at = time.utcnow_iso()



class JobManager:
    def __init__(
        self,
        *,
        max_jobs: int = 256,
        max_concurrent_jobs: int = 1,
    ) -> None:
        self.jobs: dict[str, Job] = {}
        self.max_jobs = max_jobs
        self.semaphore = asyncio.Semaphore(max_concurrent_jobs)

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        return list(self.jobs.values())

    def start(
        self,
        *,
        spec: JobSpec,
        runner: JobRunner,
        on_completed: JobCompletedHook | None = None,
    ) -> Job:
        if len(self.jobs) >= self.max_jobs:
            raise RuntimeError(f"Job limit reached: {self.max_jobs}")

        job_id = spec.job_id or f"{spec.kind}:{uuid.uuid4().hex[:8]}"

        job = Job(
            job_id=job_id,
            spec=spec,
        )

        self.jobs[job_id] = job

        asyncio.create_task(
            self._run_job(
                job_id=job_id,
                runner=runner,
                on_completed=on_completed,
            )
        )

        return job

    async def _run_job(
        self,
        *,
        job_id: str,
        runner: JobRunner,
        on_completed: JobCompletedHook | None,
    ) -> None:
        job = self.jobs[job_id]

        async with self.semaphore:
            try:
                job.update_status("running")

                result = runner(job)
                if inspect.isawaitable(result):
                    result = await result

                job.result = result

                if on_completed is not None:
                    hook_result = on_completed(job)
                    if inspect.isawaitable(hook_result):
                        await hook_result

                job.update_status("completed")

                logging.info(
                    "Job completed: job_id=%s kind=%s",
                    job.job_id,
                    job.spec.kind,
                )

            except Exception as exc:
                job.update_status("failed")
                job.error = str(exc)

                logging.error(
                    "Job failed: job_id=%s kind=%s error=%s\n%s",
                    job.job_id,
                    job.spec.kind,
                    exc,
                    traceback.format_exc(),
                )