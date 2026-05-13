import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from common.jobs import JobManager
from common.log import LogStream, LogStreamHandler
from common.primer import Primer
from common.protocol.ingress_types import (
    InferenceSession,
    LoadBackendRequest,
    LoadModelRequest,
)
from common.protocol.routing_types import WorkPacket
from cortex.cli.command_router import CommandRouter
from cortex.cluster.deployment.service import DeploymentService
from cortex.cluster.discovery.client import BackendClient
from cortex.cluster.discovery.policy import BackendRoutingPolicy
from cortex.cluster.discovery.services import DiscoveryService
from cortex.cortex.mailbox import CortexMailbox
from cortex.cortex.runtime import CortexRuntime
from cortex.router.planner import Planner
from cortex.router.service import RouterService

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
deployment_service = DeploymentService()
discovery_service = DiscoveryService()
routing_policy = BackendRoutingPolicy(discovery_service.selector)
backend_client = BackendClient()
command_router = CommandRouter(
    primer=primer,
    deployment_service=deployment_service,
    backend_service=discovery_service,
    routing_policy=routing_policy,
    backend_client=backend_client,
    job_manager=job_manager,
    log_stream=log_stream,
)
planner = Planner(routing_policy=routing_policy)
router = RouterService(
    backend_client=backend_client,
    backend_services=discovery_service,
    planner=planner,
)
cortex_mailbox = CortexMailbox(router=router)
router.set_cortex_mailbox(cortex_mailbox)
cortex_runtime = CortexRuntime(
    mailbox=cortex_mailbox,
    backend_services=discovery_service,
    primer=primer,
    command_router=command_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await discovery_service.refresher.start()
    try:
        yield
    finally:
        await discovery_service.refresher.stop()
        await primer.stop()


app = FastAPI(title="cortex", lifespan=lifespan)


@app.post("/terminal_chat")
async def terminal_chat(req: InferenceSession):
    if req.stream:
        return await cortex_runtime.handle_session_stream(req)
    else:
        return await cortex_runtime.handle_session_non_stream(req)


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


@app.post("/chat")
async def chat_test(req: InferenceSession):
    if req.stream:
        return await cortex_runtime.chat_stream(req=req)

    return await cortex_runtime.chat_non_stream(req=req)


@app.get("/network")
async def get_backends():
    backend_descriptors = discovery_service.get_registry().list_backends()

    return {"result": [descriptor.to_dict() for descriptor in backend_descriptors]}


@app.get("/network/{role}")
async def get_backends_by_role(role: str):
    candidates = routing_policy.get_all_candidates(role=role)

    return {"result": [candidate.to_dict() for candidate in candidates]}


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


@app.post("/backend")
async def load_backend(req: LoadBackendRequest):
    await primer.load_backend(req.backend)
    return {"ok": True, "primer": primer.status()}


@app.post("/unload")
async def unload_model():
    await primer.stop()
    return {"ok": True, "primer": primer.status()}


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
