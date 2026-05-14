from __future__ import annotations

import asyncio
import logging
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

from common.primer import Primer

JobStatus = Literal["accepted", "running", "completed", "failed"]
JobKind = Literal["load_model"]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class JobSpec:
    kind: JobKind
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Job:
    job_id: str
    spec: JobSpec
    status: JobStatus = "accepted"
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)

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


JobCompletedHook = Callable[
    [Job],
    Awaitable[None] | None,
]


class JobManager:
    def __init__(
        self,
        *,
        primer: Primer,
        logger: logging.Logger | None = None,
    ) -> None:
        self.primer = primer
        self.logger = logger or logging.getLogger(__name__)
        self.jobs: dict[str, Job] = {}
        self.lock = asyncio.Lock()

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        return list(self.jobs.values())

    def start(
        self,
        spec: JobSpec,
        *,
        on_completed: JobCompletedHook | None = None,
    ) -> Job:
        job_id = f"{spec.kind}:{uuid.uuid4().hex[:8]}"

        job = Job(
            job_id=job_id,
            spec=spec,
        )

        self.jobs[job_id] = job

        asyncio.create_task(
            self._run_job(
                job_id=job_id,
                on_completed=on_completed,
            )
        )

        return job

    async def _run_job(
        self,
        *,
        job_id: str,
        on_completed: JobCompletedHook | None,
    ) -> None:
        job = self.jobs[job_id]

        async with self.lock:
            try:
                job.status = "running"
                job.updated_at = utcnow_iso()

                if job.spec.kind == "load_model":
                    await self._run_load_model_job(job)
                else:
                    raise ValueError(f"Unsupported job kind: {job.spec.kind}")

                if on_completed is not None:
                    result = on_completed(job)
                    if asyncio.iscoroutine(result):
                        await result

                job.status = "completed"
                job.updated_at = utcnow_iso()

                self.logger.info(
                    "Job completed: job_id=%s kind=%s",
                    job.job_id,
                    job.spec.kind,
                )

            except Exception as exc:
                job.status = "failed"
                job.error = str(exc)
                job.updated_at = utcnow_iso()

                self.logger.error(
                    "Job failed: job_id=%s kind=%s error=%s\n%s",
                    job.job_id,
                    job.spec.kind,
                    exc,
                    traceback.format_exc(),
                )

    async def _run_load_model_job(self, job: Job) -> None:
        payload = job.spec.payload

        model_id = payload["model_id"]
        provider = payload.get("provider") or "huggingface"
        engine = payload.get("engine")
        repo_id = payload.get("repo_id")
        filename = payload.get("filename")
        revision = payload.get("revision", "main")
        tokenizer_id = payload.get("tokenizer_id")
        force_reload = payload.get("force_reload", False)

        self.logger.info(
            "Model load started: job_id=%s model=%s engine=%s",
            job.job_id,
            model_id,
            engine,
        )

        if engine:
            current_engine = self.primer.status().get("engine")

            if current_engine != engine:
                self.logger.info(
                    "Switching engine: %s -> %s",
                    current_engine,
                    engine,
                )
                await self.primer.load_engine(engine)

        active_engine = self.primer.status().get("engine")

        if active_engine == "gguf":
            if not repo_id or not filename:
                raise ValueError("GGUF engine requires repo_id and filename")

            self.logger.info(
                "Loading GGUF model: repo=%s file=%s revision=%s",
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
            self.logger.info(
                "Loading model: model=%s backend=%s provider=%s",
                model_id,
                active_engine,
                provider,
            )

            await self.primer.load_model(
                model_id,
                force_reload=force_reload,
                provider=provider,
            )

        job.result = {
            "model_id": model_id,
            "engine": active_engine,
            "primer": self.primer.status(),
        }
