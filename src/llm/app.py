from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from common.jobs import JobManager, JobSpec
from common.log import LogStream, LogStreamHandler
from common.primer import Primer
from common.protocol.ingress_types import LoadBackendRequest, LoadModelRequest
from common.protocol.routing_types import WorkPacket
from common.system import configuration
from llm.runtime import GenerativeModelRuntime

log_stream = LogStream()

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

has_stdout_handler = any(
    isinstance(h, logging.StreamHandler) and not isinstance(h, LogStreamHandler)
    for h in root_logger.handlers
)

if not has_stdout_handler:
    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s")
    )
    root_logger.addHandler(stdout_handler)

has_attach_handler = any(isinstance(h, LogStreamHandler) for h in root_logger.handlers)

if not has_attach_handler:
    attach_handler = LogStreamHandler(log_stream)
    attach_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s")
    )
    root_logger.addHandler(attach_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):

    configuration.load_configuration_defaults("llm")

    app.state.internal_http = httpx.AsyncClient(
        timeout=httpx.Timeout(180.0, connect=2.0),
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        ),
        verify=False,
        trust_env=False,
    )

    app.state.external_http = httpx.AsyncClient(
        timeout=30.0,
        verify=True,
        trust_env=True,
        follow_redirects=True,
    )

    app.state.primer = Primer(external_http=app.state.external_http, cfg="llm")
    app.state.job_manager = JobManager()
    app.state.generative_runtime = GenerativeModelRuntime(
        primer=app.state.primer, job_manager=app.state.job_manager
    )

    root_logger.info("Starting LLM app")
    try:
        yield
    finally:
        root_logger.info("Stopping LLM app")
        await app.state.primer.stop()


app = FastAPI(title="llm", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def runtime(request: Request) -> GenerativeModelRuntime:
    return request.app.state.generative_runtime


def primer(request: Request) -> Primer:
    return request.app.state.primer


def job_manager(request: Request) -> JobManager:
    return request.app.state.job_manager


# ---------------------------------------------------------------------------
# Basic service endpoints
# ---------------------------------------------------------------------------


@app.get("/live")
async def live():
    return {"ok": True}


@app.get("/ready")
async def ready(request: Request):
    primer = request.app.state.primer
    return {
        "ok": True,
        "accepting_requests": True,
        "primer": primer.status(),
    }


@app.get("/health")
async def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Network endpoints
# ---------------------------------------------------------------------------


@app.post("/engine")
async def load_backend(req: LoadBackendRequest, request: Request):
    _primer = primer(request=request)
    await _primer.load_engine(req.engine)
    return {"ok": True, "primer": _primer.status()}


@app.post("/work")
async def submit_work(req: WorkPacket, request: Request):
    _primer = primer(request=request)
    generative_runtime = runtime(request=request)
    if not _primer.is_ready():
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "primer_not_ready",
                "primer": _primer.status(),
            },
        )

    return await generative_runtime.handle_ingress(req)


@app.get("/work/{work_id}")
async def fetch_work(work_id: str, request: Request):
    generative_runtime = runtime(request=request)
    return await generative_runtime.handle_egress(work_id)


@app.post("/load")
async def load_model(req: LoadModelRequest, request: Request):
    _primer = primer(request=request)
    _job_manager = job_manager(request=request)
    _runtime = runtime(request=request)

    engine = req.engine or _primer.status().get("engine")

    if not engine:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "No engine selected. Provide engine or load an engine first.",
            },
        )

    if engine == "gguf" and (not req.repo_id or not req.filename):
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "GGUF engine requires repo_id and filename",
            },
        )

    payload = req.model_dump(mode="python")
    payload["engine"] = engine

    spec = JobSpec(
        kind="llm.load_model",
        payload=payload,
    )

    job = _job_manager.start(
        spec=spec,
        runner=_runtime.run_load_model_job,
    )

    return JSONResponse(
        status_code=202,
        content={
            "ok": True,
            "status": job.status,
            "job_id": job.job_id,
            "kind": job.spec.kind,
            "model_id": req.model_id,
            "engine": engine,
        },
    )


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    _job_manager = job_manager(request=request)
    job = _job_manager.get(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    return job.to_dict()


@app.post("/unload")
async def unload_model(request: Request):
    _primer = primer(request=request)
    await _primer.stop()
    return {"ok": True, "primer": _primer.status()}


@app.get("/model_status")
async def models(request: Request):
    _primer = primer(request=request)
    status = _primer.status()
    state = status.get("state")

    if state == "ready":
        return {
            "ok": True,
            "model": {
                "id": status.get("model_id") or status.get("model_path"),
                "engine": status.get("engine"),
                "effective_n_ctx": status.get("effective_n_ctx", 0),
                "n_gpu_layers": status.get("n_gpu_layers"),
                "n_batch": status.get("n_batch"),
                "runtime_profile": status.get("runtime_profile"),
            },
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
