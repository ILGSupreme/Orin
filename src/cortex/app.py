import cProfile
import io
import logging
import pstats
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from common.jobs import Job, JobManager, JobSpec
from common.log import LogStream, LogStreamHandler
from common.primer import Primer, execute_load_model
from common.protocol.ingress_types import (
    InferenceSession,
    LoadBackendRequest,
    LoadModelRequest,
)
from common.protocol.routing_types import WorkPacket
from common.system import configuration
from cortex.cli.command_router import CommandRouter
from cortex.cluster.deployment.service import DeploymentService
from cortex.cluster.deployment.inventory import InventoryUnavailable
from cortex.cluster.discovery.client import BackendClient
from cortex.cluster.discovery.policy import BackendRoutingPolicy
from cortex.cluster.discovery.services import DiscoveryService
from cortex.cortex.harness import Harness
from cortex.cortex.agent import AgentRuntime
from cortex.cortex.actions import ActionRegistry
from cortex.cortex.inference import InferenceGateway, InferenceUnavailable
from cortex.cortex.goal_store import GoalStore
from common.protocol.agent_types import AgentLimits, GoalControl
from cortex.cortex import memory as cortex_memory
from cortex.cortex.runtime import CortexRuntime
from cortex.router.planner import Planner
from cortex.router.service import RouterService

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

    configuration.load_configuration_file("cortex")

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

    app.state.primer = Primer(external_http=app.state.external_http, cfg="cortex")

    app.state.job_manager = JobManager()

    app.state.deployment_service = DeploymentService()

    app.state.discovery_service = DiscoveryService(http=app.state.internal_http)

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
        job_manager=app.state.job_manager,
        planner=app.state.planner,
    )

    cfg = configuration.get_configuration("cortex")
    app.state.inference = InferenceGateway(cfg.inference, primer=app.state.primer,
        router=app.state.router, http=app.state.external_http)
    store = GoalStore(http=app.state.internal_http,
        base_url=cfg.inventory_memory_base_url or f"http://memory-service.{cfg.namespace}.svc.cluster.local:8080",
        namespace=cfg.namespace)
    actions = ActionRegistry(inference=app.state.inference, router=app.state.router,
        commands=app.state.command_router, tools=cfg.agent_tools)
    async def conversation_context(request):
        return await cortex_memory.conversation_context(app.state.router, request, cfg.inference.default_privacy)
    app.state.agent = AgentRuntime(inference=app.state.inference, actions=actions,
        store=store, jobs=app.state.job_manager, limits=AgentLimits(**cfg.agent_limits),
        default_privacy=cfg.inference.default_privacy, context_loader=conversation_context)
    app.state.command_router.agent_runtime = app.state.agent
    app.state.harness = Harness(router=app.state.router,
        job_manager=app.state.job_manager, agent_runtime=app.state.agent)

    app.state.cortex_runtime = CortexRuntime(
        harness=app.state.harness,
        discovery_service=app.state.discovery_service,
        primer=app.state.primer,
        command_router=app.state.command_router,
    )

    await app.state.discovery_service.refresher.start()

    try:
        try:
            await app.state.deployment_service.initialize_inventory(app.state.internal_http)
        except InventoryUnavailable as exc:
            root_logger.warning("Inventory startup load failed: %s", exc)
        app.state.agent.start()
        yield
    finally:
        await app.state.agent.close()
        await app.state.job_manager.shutdown()
        await app.state.discovery_service.refresher.stop()
        await app.state.primer.stop()

        await app.state.external_http.aclose()
        await app.state.internal_http.aclose()


app = FastAPI(title="cortex", lifespan=lifespan)


@app.exception_handler(InferenceUnavailable)
async def inference_unavailable(request: Request, exc: InferenceUnavailable):
    return JSONResponse(
        status_code=503,
        content={"detail": "Configured inference could not complete the request. Check backend availability and operator logs."},
    )


@app.get("/goals")
async def goals(request: Request, user_id: str, session_id: str):
    values = await request.app.state.agent.store.list(user_id=user_id, session_id=session_id)
    return {"goals": [goal.model_dump(mode="json", exclude={"lease_owner", "lease_until", "messages"}) for goal in values]}


@app.post("/goals/{goal_id}/control")
async def control_goal(goal_id: str, body: GoalControl, request: Request):
    try:
        text = await request.app.state.agent.control(goal_id, body.user_id, body.session_id,
            body.action, task_id=body.task_id, feedback=body.feedback, event_key=body.event_key)
        return {"message": text}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail="Goal storage is unavailable or changed; reload before retrying") from exc

# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def cortex(request: Request) -> CortexRuntime:
    return request.app.state.cortex_runtime


def primer(request: Request) -> Primer:
    return request.app.state.primer


def discovery(request: Request) -> DiscoveryService:
    return request.app.state.discovery_service


def deployment(request: Request) -> DeploymentService:
    return request.app.state.deployment_service


def policy(request: Request) -> BackendRoutingPolicy:
    return request.app.state.routing_policy


def job_manager(request: Request) -> JobManager:
    return request.app.state.job_manager


def command_router(request: Request) -> CommandRouter:
    return request.app.state.command_router


# ---------------------------------------------------------------------------
# Basic service endpoints
# ---------------------------------------------------------------------------


@app.get("/live")
async def live():
    return {"ok": True}


@app.get("/ready")
async def ready(request: Request):
    _primer = primer(request=request)
    return {
        "ok": True,
        "accepting_requests": request.app.state.inference.status()["ready"] and request.app.state.agent.storage_error is None,
        "inference": request.app.state.inference.status(),
        "goal_storage_error": request.app.state.agent.storage_error,
        "primer": _primer.status(),
        "inventory": deployment(request).inventory_status(),
    }


@app.get("/health")
async def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Network endpoints
# ---------------------------------------------------------------------------


@app.post("/terminal_chat")
async def terminal_chat(req: InferenceSession, request: Request):
    cortex_runtime = cortex(request=request)

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

    pstats.Stats(profiler, stream=total_buffer).strip_dirs().sort_stats(
        "tottime"
    ).print_stats(25)

    pstats.Stats(profiler, stream=cumulative_buffer).strip_dirs().sort_stats(
        "cumtime"
    ).print_stats(25)

    root_logger.info("terminal_chat profile by tottime:\n%s", total_buffer.getvalue())
    root_logger.info("terminal_chat profile by cumtime:\n%s", cumulative_buffer.getvalue())

    #logging.info("terminal_chat profile by tottime:\n%s", total_buffer.getvalue())
    #logging.info("terminal_chat profile by cumtime:\n%s", cumulative_buffer.getvalue())

    return content


@app.post("/work")
async def submit_work(req: WorkPacket, request: Request):
    cortex_runtime = cortex(request=request)
    return await cortex_runtime.handle_ingress(req)


@app.get("/work/{work_id}")
async def fetch_work(work_id: str, request: Request):
    cortex_runtime = cortex(request=request)
    return await cortex_runtime.handle_egress(work_id)


@app.post("/chat")
async def chat(req: InferenceSession, request: Request):
    cortex_runtime = cortex(request=request)
    if req.stream:
        return await cortex_runtime.chat_stream(req=req)

    return await cortex_runtime.chat_non_stream(req=req)


@app.get("/network")
async def get_backends(request: Request):
    discovery_service = discovery(request=request)
    backend_descriptors = discovery_service.get_registry().list_backends()

    return {"result": [descriptor.to_dict() for descriptor in backend_descriptors]}


@app.get("/network/{role}")
async def get_backends_by_role(role: str, request: Request):
    routing_policy = policy(request=request)
    candidates = routing_policy.get_all_candidates(role=role)

    return {"result": [candidate.to_dict() for candidate in candidates]}


@app.post("/load")
async def load_model(req: LoadModelRequest, request: Request):
    _primer = primer(request=request)
    _job_manager = job_manager(request=request)
    _deployment = deployment(request=request)
    _command_router = command_router(request=request)

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

    def update_cortex_discovery(job: Job):
        metadata = job.latest_result.get("metadata", {})
        model_id = metadata.get("model_id")

        service_name = _command_router._service_name_from_target("cortex")

        _deployment.update_service_discovery_metadata(
            service_name=service_name,
            model=model_id,
        )

    payload = req.model_dump(mode="python")
    payload["engine"] = engine

    spec = JobSpec(
        kind="cortex.load_model",
        payload=payload,
    )

    job = _job_manager.start(
        spec=spec,
        runner=lambda job: execute_load_model(job, _primer),
        on_completed=update_cortex_discovery,
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


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    _job_manager = job_manager(request=request)

    job = _job_manager.get(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    return job.to_dict()


@app.post("/engine")
async def load_backend(req: LoadBackendRequest, request: Request):
    _primer = primer(request=request)
    await _primer.load_engine(req.engine)
    return {"ok": True, "primer": _primer.status()}


@app.post("/unload")
async def unload_model(request: Request):
    _primer = primer(request=request)

    await _primer.stop()
    return {"ok": True, "primer": _primer.status()}


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
