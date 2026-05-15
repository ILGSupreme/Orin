import logging
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, HTTPException,Request
from fastapi.responses import JSONResponse, StreamingResponse
import io
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
from common.system import configuration
from cortex.router.service import RouterService
import cProfile
import pstats

log_stream = LogStream()

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

has_stdout_handler = any(
    isinstance(h, logging.StreamHandler)
    and not isinstance(h, LogStreamHandler)
    for h in root_logger.handlers
)

if not has_stdout_handler:
    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s")
    )
    root_logger.addHandler(stdout_handler)

has_attach_handler = any(
    isinstance(h, LogStreamHandler)
    for h in root_logger.handlers
)

if not has_attach_handler:
    attach_handler = LogStreamHandler(log_stream)
    attach_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s")
    )
    root_logger.addHandler(attach_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
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

    configuration.load_configuration_file("cortex")

    app.state.primer = Primer(
        external_http=app.state.external_http,
    )

    app.state.job_manager = JobManager(
        primer=app.state.primer,
    )

    app.state.deployment_service = DeploymentService()

    app.state.discovery_service = DiscoveryService()
    # If DiscoveryService / health probing uses httpx internally,
    # change its constructor to accept the shared client:
    #
    # app.state.discovery_service = DiscoveryService(
    #     http=app.state.internal_http,
    # )

    app.state.routing_policy = BackendRoutingPolicy(
        app.state.discovery_service.selector,
    )

    app.state.backend_client = BackendClient(
        http=app.state.internal_http,
    )

    app.state.command_router = CommandRouter(
        primer=app.state.primer,
        deployment_service=app.state.deployment_service,
        backend_service=app.state.discovery_service,
        routing_policy=app.state.routing_policy,
        backend_client=app.state.backend_client,
        job_manager=app.state.job_manager,
        log_stream=log_stream,
    )

    app.state.planner = Planner(
        routing_policy=app.state.routing_policy,
    )

    app.state.router = RouterService(
        backend_client=app.state.backend_client,
        backend_services=app.state.discovery_service,
        planner=app.state.planner,
    )

    app.state.cortex_mailbox = CortexMailbox(
        router=app.state.router,
    )

    app.state.router.set_cortex_mailbox(
        app.state.cortex_mailbox,
    )

    app.state.cortex_runtime = CortexRuntime(
        mailbox=app.state.cortex_mailbox,
        backend_services=app.state.discovery_service,
        primer=app.state.primer,
        command_router=app.state.command_router,
    )

    await app.state.discovery_service.refresher.start()

    try:
        yield
    finally:
        await app.state.discovery_service.refresher.stop()
        await app.state.primer.stop()

        await app.state.external_http.aclose()
        await app.state.internal_http.aclose()


app = FastAPI(title="cortex", lifespan=lifespan)



@app.post("/terminal_chat")
async def terminal_chat(req: InferenceSession, request: Request):
    cortex_runtime = request.app.state.cortex_runtime

    if req.stream:
        return await cortex_runtime.handle_session_stream(req)
    
    ### PROFILER
    profiler = cProfile.Profile()
    profiler.enable()

    try:
        content = await cortex_runtime.handle_session_non_stream(req)
    finally:
        profiler.disable()

    total_buffer = io.StringIO()
    cumulative_buffer = io.StringIO()

    pstats.Stats(profiler, stream=total_buffer) \
        .strip_dirs() \
        .sort_stats("tottime") \
        .print_stats(25)

    pstats.Stats(profiler, stream=cumulative_buffer) \
        .strip_dirs() \
        .sort_stats("cumtime") \
        .print_stats(25)

    logging.info("terminal_chat profile by tottime:\n%s", total_buffer.getvalue())
    logging.info("terminal_chat profile by cumtime:\n%s", cumulative_buffer.getvalue())

    return content


@app.post("/work")
async def submit_work(req: WorkPacket, request:Request):

    cortex_runtime = request.app.state.cortex_runtime
    primer = request.app.state.primer

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
async def fetch_work(work_id: str, request: Request):
    cortex_runtime = request.app.state.cortex_runtime
    return await cortex_runtime.handle_egress(work_id)


@app.post("/chat")
async def chat_test(req: InferenceSession, request: Request):
    cortex_runtime = request.app.state.cortex_runtime
    if req.stream:
        return await cortex_runtime.chat_stream(req=req)

    return await cortex_runtime.chat_non_stream(req=req)


@app.get("/network")
async def get_backends(request:Request):
    discovery_service = request.app.state.discovery_service
    backend_descriptors = discovery_service.get_registry().list_backends()

    return {"result": [descriptor.to_dict() for descriptor in backend_descriptors]}


@app.get("/network/{role}")
async def get_backends_by_role(role: str, request:Request):
    routing_policy = request.app.state.routing_policy
    candidates = routing_policy.get_all_candidates(role=role)

    return {"result": [candidate.to_dict() for candidate in candidates]}


@app.post("/load")
async def load_model(req: LoadModelRequest, request: Request):
    primer = request.app.state.primer
    job_manager = request.app.state.job_manager
    backend = req.engine or primer.status().get("backend")

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
async def get_job(job_id: str, request: Request):
    job_manager = request.app.state.job_manager

    job = job_manager.get(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    return job.to_dict()


@app.post("/engine")
async def load_backend(req: LoadBackendRequest, request: Request):
    primer = request.app.state.primer
    await primer.load_engine(req.engine)
    return {"ok": True, "primer": primer.status()}


@app.post("/unload")
async def unload_model(request: Request):
    primer = request.app.state.primer

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
