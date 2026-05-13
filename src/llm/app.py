from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from common.jobs import JobManager
from common.log import LogStream, LogStreamHandler
from common.primer import Primer
from common.protocol.ingress_types import LoadBackendRequest, LoadModelRequest
from common.protocol.routing_types import WorkPacket
from llm.runtime import GenerativeModelRuntime

log_stream = LogStream()
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

has_stdout_handler = any(
    isinstance(h, logging.StreamHandler) for h in root_logger.handlers
)

if not has_stdout_handler:
    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s")
    )
    root_logger.addHandler(stdout_handler)

attach_handler = LogStreamHandler(log_stream)
attach_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s")
)
root_logger.addHandler(attach_handler)

primer = Primer()
job_manager = JobManager(primer=primer)
cortex_runtime = GenerativeModelRuntime(primer=primer)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Starting LLM app")
    try:
        yield
    finally:
        logging.info("Stopping LLM app")
        await primer.stop()


app = FastAPI(title="cortex", lifespan=lifespan)


@app.post("/backend")
async def load_backend(req: LoadBackendRequest):
    await primer.load_backend(req.backend)
    return {"ok": True, "primer": primer.status()}


@app.post("/work")
async def submit_work(req: WorkPacket):
    if not primer.is_ready():
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "primer_not_ready",
                "primer": primer.status(),
            },
        )

    return await cortex_runtime.handle_ingress(req)


@app.get("/work/{work_id}")
async def fetch_work(work_id: str):
    return await cortex_runtime.handle_egress(work_id)


@app.post("/load")
async def load_model(req: LoadModelRequest):
    backend = req.backend or primer.status().get("backend")

    if backend == "gguf" and (not req.repo_id or not req.filename):
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "GGUF backend requires repo_id and filename",
            },
        )

    spec = req.to_job_spec()
    spec.payload["backend"] = backend

    job = job_manager.start(spec)

    return {
        "ok": True,
        "status": job.status,
        "job_id": job.job_id,
        "kind": job.spec.kind,
        "model_id": req.model_id,
        "backend": backend,
    }


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = job_manager.get(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    return job.to_dict()


@app.post("/unload")
async def unload_model():
    await primer.stop()
    return {"ok": True, "primer": primer.status()}


@app.get("/models")
async def models():
    status = primer.status()
    state = status.get("state")

    if state == "ready":
        return {
            "ok": True,
            "models": [
                {
                    "id": status.get("model_id") or status.get("model_path"),
                    "backend": status.get("backend"),
                    "effective_n_ctx": status.get("effective_n_ctx", 0),
                    "n_gpu_layers": status.get("n_gpu_layers"),
                    "n_batch": status.get("n_batch"),
                    "runtime_profile": status.get("runtime_profile"),
                }
            ],
        }

    return {
        "ok": False,
        "models": [],
    }


@app.get("/attach")
async def attach(tail: int = 100):
    return StreamingResponse(
        log_stream.stream(tail=tail),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/live")
async def live():
    return {"ok": True}


@app.get("/ready")
async def ready():
    return {
        "ok": True,
        "accepting_requests": True,
        "primer": primer.status(),
    }


@app.get("/health")
async def health():
    return {"ok": True}
