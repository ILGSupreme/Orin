from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, overload

from fastapi.responses import StreamingResponse
from pydantic import TypeAdapter

from common.factory import packing
from common.jobs import Job, JobManager, JobSpec, PipelineStage
from common.presentation import formatting
from common.primer import Primer
from common.protocol import unified_types
from common.protocol.egress_types import EgressResponse
from common.protocol.ingress_types import InferenceSession
from common.protocol.internal_types import InferenceObject
from common.protocol.routing_types import (
    RoutingHints,
    WorkDisposition,
    WorkPacket,
    WorkResult,
    WorkType,
)
from common.protocol.unified_types import (
    ContentPart,
    PromptContextResponse,
    RuntimeMessage,
)
from common.types import MAX_TOKENS_POLICY, TEMPERATURE_POLICY
from cortex.cli.command_router import CommandRouter
from cortex.cortex import memory, prompts
from cortex.cortex.harness_types import (
    LIGHTWEIGHT_INGRESS_ADAPTER,
    IngressMode,
    LightweightIngressInterpretationModel,
)
from cortex.cortex.prompts import (
    build_final_response_messages,
    build_task_messages,
    build_task_shaping_messages,
)
from cortex.router.service import RouterService

SystemInformationMode = Literal["lightweight", "response", "full"]
SystemInformationPurpose = Literal["interpretation", "response"]

root_logger = logging.getLogger()

@dataclass(slots=True)
class ResponsePlan:
    system_summary: str
    ingress_interpretation: LightweightIngressInterpretationModel
    background_job: Job | None = None
    direct_text: str | None = None
    respond_directly: bool = False


class Harness:
    def __init__(
        self,
        primer: Primer,
        router: RouterService,
        job_manager: JobManager,
        command_router: CommandRouter,
    ) -> None:
        self.primer = primer
        self.router = router
        self.job_manager = job_manager
        self.command_router = command_router

        self.job_manager.define_pipeline(
            "chat",
            stages=[
                PipelineStage(
                    name="get_prompt",
                    runner=lambda job: execute_get_prompt(job, self.router),
                    timeout_seconds=30,
                ),
                PipelineStage(
                    name="interpret_turn",
                    runner=lambda job: execute_interpret_turn(
                        job, self.router, self.primer
                    ),
                    poller=lambda job, result: execute_read_interpret(
                        job, self.router, result
                    ),
                    timeout_seconds=90,
                    poll_interval_seconds=2.0,
                ),
                PipelineStage(
                    name="shape_turn",
                    runner=lambda job: execute_shape_tasks(
                        job, self.router, self.primer
                    ),
                    poller=lambda job, result: execute_read_shaping(
                        job, self.router, result
                    ),
                    timeout_seconds=180,
                    poll_interval_seconds=2.0,
                ),
                PipelineStage(
                    name="build_packets",
                    runner=lambda job: execute_build_work_packets(
                        job, self.router, self.primer
                    ),
                    timeout_seconds=180,
                ),
                PipelineStage(
                    name="read_and_send_packets",
                    runner=lambda job: execute_read_and_send_packets(
                        job, self.router, self.job_manager
                    ),
                    timeout_seconds=180,
                ),
                PipelineStage(
                    name="build_final_response_message",
                    runner=lambda job: execute_final_response(
                        job, self.router, self.primer
                    ),
                    poller=lambda job, result: execute_read_final_packet(
                        job, self.router, result
                    ),
                    timeout_seconds=180,
                    poll_interval_seconds=2.0,
                ),
            ],
        )

        ## work pipeline
        self.job_manager.define_pipeline(
            "work_pipeline",
            stages=[
                PipelineStage(
                    name="work_packet",
                    runner=lambda job: execute_work_packet(job, self.router),
                    poller=lambda job, result: execute_retrieve_work_packet(
                        job, self.router, result
                    ),
                    timeout_seconds=90,
                    poll_interval_seconds=2.0,
                ),
            ],
        )

    @overload
    async def ingression(
        self,
        ingression_type: IngressMode,
        request: InferenceSession,
        stream: Literal[True],
    ) -> StreamingResponse: ...
    @overload
    async def ingression(
        self,
        ingression_type: IngressMode,
        request: InferenceSession,
        stream: Literal[False],
    ) -> EgressResponse: ...

    async def ingression(
        self,
        ingression_type: IngressMode,
        request: InferenceSession,
        stream: bool = False,
    ) -> StreamingResponse | EgressResponse:
        _inference_object: InferenceObject = await self._validate_session(req=request)

        match ingression_type:
            case "terminal":
                return await self._response(_inference_object, "terminal")
            case "chat":
                return await self._response(_inference_object, "chat")

    async def handle_work_packet(self, packet: WorkPacket):
        if packet.task.work_type == "llm":
            job = self.job_manager.start_pipeline(
                spec=JobSpec(
                    kind="harness.work",
                    payload={"packet": packet.model_dump(mode="json")},
                ),
                pipeline="work_pipeline",
            )
            return WorkResult(
                status="accepted",
                work_id=job.job_id,
                metadata={
                    "original_work_id": packet.work_id,
                    "job_id": job.job_id,
                    "kind": job.spec.kind,
                },
            )
        return WorkResult(
            status="failed", work_id=packet.work_id, error="unknown work type"
        )

    async def retrieve_work_packet(self, work_id: str) -> WorkResult:
        job = self.job_manager.get(job_id=work_id)

        if not job:
            return WorkResult(
                status="failed",
                work_id=work_id,
                error="job not found",
            )

        if job.status not in ("completed", "failed"):
            return WorkResult(
                status=job.status,
                work_id=job.job_id,
                metadata={
                    "job_id": job.job_id,
                    "kind": job.spec.kind,
                    "stage": getattr(job, "stage", None),
                },
            )

        if job.get_result() is None:
            return WorkResult(
                status="failed",
                work_id=job.job_id,
                error="job completed without result",
            )
        
        result = WorkResult.model_validate(job.get_result())

        return result.sanitize_for_federation()

    # ---------------------------------------------------------------------------
    # Private Class Function Calls
    # ---------------------------------------------------------------------------

    async def _validate_session(self, req: InferenceSession) -> InferenceObject:
        try:
            _iobj = InferenceObject.model_validate(req.model_dump())

            rsp = await memory.upsert_user(router=self.router, inference_object=_iobj)

            if not rsp:
                root_logger.info("failed to create/update user")
                #logging.info("failed to create/update user")
                raise ValueError("Memory service did not create/update user")

            session_id = _iobj.session_id
            if not session_id:
                rsp = await memory.resolve_session(
                    router=self.router, inference_object=_iobj
                )
                session = rsp.get("session", {})
                if session:
                    session_id = session.get("id")
                if not session_id:
                    raise ValueError("Memory service did not return a session id")
                _iobj.session_id = session_id

            responses = await memory.insert_message(
                router=self.router, inference_object=_iobj, message=_iobj.content
            )
            if any(rsp.status == "failed" for rsp in responses):
                root_logger.info(f"Encountered failed responses: {responses}")
                #logging.info(f"Encountered failed responses: {responses}")

            return _iobj
        except Exception:
            root_logger.exception("Validate Session fault")
            raise

    # ---------------------------------------------------------------------------
    # Private Response Functions
    # ---------------------------------------------------------------------------

    async def _response(
        self,
        inference_object: InferenceObject,
        prompt_mode: Literal["terminal", "chat"],
    ):
        match prompt_mode:
            case "terminal":
                plan = await self._response_terminal(
                    inference_object=inference_object,
                    prompt_mode="terminal",
                )
            case "chat":
                plan = await self._response_chat(
                    inference_object=inference_object,
                    prompt_mode="chat",
                )
            case _:
                raise ValueError("unknown prompt mode")

        if plan.respond_directly:
            if inference_object.stream:
                return StreamingResponse(
                    _output_generator(plan.direct_text),
                    media_type="text/plain",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "Session-Id": inference_object.session_id
                        if inference_object.session_id
                        else "",
                    },
                )
            else:
                return EgressResponse(
                    content=[
                        RuntimeMessage(
                            role="assistant",
                            parts=[
                                ContentPart(
                                    type="text",
                                    data=plan.direct_text or "",
                                    encoding="plain",
                                    mime_type="text/plain",
                                )
                            ],
                            metadata={
                                "visibility": "user",
                                "kind": "final",
                            },
                        )
                    ],
                    session_id=inference_object.session_id,
                    metadata={
                        "stream": False,
                        "mode": "direct_response",
                    },
                )

        prompt_context = await memory.get_prompts_context(
            router=self.router,
            inference_object=inference_object,
            limit=1,
            message_roles=["assistant"],
        )

        last_assistant_message = (
            prompt_context.get_latest_message() if prompt_context else None
        )

        runtime_messages = prompts.build_fast_response_messages(
            messages=inference_object.content,
            last_assistant_message=last_assistant_message,
            prompt_mode=prompt_mode,
            ingress_interpretation=plan.ingress_interpretation,
            system_information=plan.system_summary,
            background_job_id=plan.background_job.job_id
            if plan.background_job
            else None,
        )

        reserved_output_tokens = MAX_TOKENS_POLICY.get("chat", 256)

        effective_tokens = self.primer._get_total_token_estimation(
            reserved_output_tokens=reserved_output_tokens,
            messages=runtime_messages
        )

        root_logger.info(f"estimated tokens: {effective_tokens}")

        fits = self.primer._can_fit_request(
            effective_n_ctx=self.primer.status()["effective_n_ctx"],
            reserved_output_tokens=reserved_output_tokens,
            messages=runtime_messages,
        )

        if not fits:
            root_logger.error("Message can not be inferred correctly by backend")

        if inference_object.stream:
            return StreamingResponse(
                self.primer.stream_text(
                    messages=runtime_messages,
                    constraints={"temperature": TEMPERATURE_POLICY.get("chat")},
                    operation="chat",
                ),
                media_type="text/plain",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Session-Id": inference_object.session_id
                    if inference_object.session_id
                    else "",
                },
            )

        response_messages = await self.primer.chat_text_message(
            messages=runtime_messages,
            constraints={"temperature": TEMPERATURE_POLICY.get("chat")},
            operation="chat",
        )

        return EgressResponse(
            content=response_messages,
            session_id=inference_object.session_id,
            metadata={
                "stream": False,
                "mode": "fast_response",
            },
        )

    async def _response_terminal(
        self,
        inference_object: InferenceObject,
        prompt_mode: IngressMode,
    ) -> ResponsePlan:
        system_summary = await self.collate_system_information(
            inference_object=inference_object,
            prompt_mode=prompt_mode,
            purpose="interpretation",
        )

        ingress_interpretation = await self._lightweight_interpret(
            inference_object=inference_object,
            prompt_mode="terminal",
            system_summary=system_summary,
        )

        terminal_action_result = None

        if ingress_interpretation.mode == "terminal":
            if ingress_interpretation.action == "terminal_info_action":
                terminal_action = ingress_interpretation.terminal_action

                if terminal_action is None:
                    raise ValueError("terminal_info_action requires terminal_action")

                terminal_action_result = (
                    await self.command_router.execute_harness_action(
                        terminal_action,
                        mode="terminal",
                    )
                )

                return ResponsePlan(
                    system_summary="",
                    ingress_interpretation=ingress_interpretation,
                    direct_text=terminal_action_result,
                    respond_directly=True,
                )

            if ingress_interpretation.action == "report_job_status":
                if not inference_object.session_id:
                    raise ValueError("No session id")

                jobs = self.job_manager.list_jobs_for_session(
                    session_id=inference_object.session_id
                )

                text = formatting.format_session_job_status(jobs=jobs)

                return ResponsePlan(
                    system_summary="",
                    ingress_interpretation=ingress_interpretation,
                    direct_text=text,
                    respond_directly=True,
                )

            if ingress_interpretation.action == "get_job_result":
                if not inference_object.session_id:
                    raise ValueError("No session id")

                jobs = self.job_manager.list_jobs_for_session(
                    session_id=inference_object.session_id
                )

                text = formatting.format_latest_job_result(jobs=jobs)

                return ResponsePlan(
                    system_summary="",
                    ingress_interpretation=ingress_interpretation,
                    direct_text=text,
                    respond_directly=True,
                )

        response_summary = await self.collate_system_information(
            inference_object=inference_object,
            prompt_mode=prompt_mode,
            purpose="response",
            terminal_action_result=terminal_action_result,
            background_job_id="",
        )

        return ResponsePlan(
            system_summary=response_summary,
            ingress_interpretation=ingress_interpretation,
        )

    async def _response_chat(
        self,
        inference_object: InferenceObject,
        prompt_mode: IngressMode,
    ) -> ResponsePlan:
        system_summary = await self.collate_system_information(
            inference_object=inference_object,
            prompt_mode=prompt_mode,
            purpose="interpretation",
        )

        ingress_interpretation = await self._lightweight_interpret(
            inference_object=inference_object,
            prompt_mode=prompt_mode,
            system_summary=system_summary,
        )

        background_job = None

        if ingress_interpretation.mode == "chat":
            if ingress_interpretation.action == "start_pipeline":
                pipeline = ingress_interpretation.pipeline

                if pipeline is None:
                    raise ValueError("chat start_pipeline action requires pipeline")

                background_job = self.job_manager.start_pipeline(
                    spec=JobSpec(
                        kind="harness.chat",
                        payload={
                            "user_id": inference_object.user_id,
                            "session_id": inference_object.session_id,
                            "channel": inference_object.channel,
                            "inference_object": inference_object.model_dump(),
                        },
                    ),
                    pipeline=pipeline,
                )

                text = (
                    f"A deeper response is being worked on. "
                    f"Background job `{background_job.job_id}` has been started. "
                    "Ask for the status or result later."
                )

                return ResponsePlan(
                    system_summary="",
                    ingress_interpretation=ingress_interpretation,
                    background_job=background_job,
                    direct_text=text,
                    respond_directly=True,
                )

            if ingress_interpretation.action == "report_job_status":
                if not inference_object.session_id:
                    raise ValueError("No session id")

                jobs = self.job_manager.list_jobs_for_session(
                    session_id=inference_object.session_id
                )

                text = formatting.format_session_job_status(jobs=jobs)

                return ResponsePlan(
                    system_summary="",
                    ingress_interpretation=ingress_interpretation,
                    direct_text=text,
                    respond_directly=True,
                )

            if ingress_interpretation.action == "get_job_result":
                if not inference_object.session_id:
                    raise ValueError("No session id")

                jobs = self.job_manager.list_jobs_for_session(
                    session_id=inference_object.session_id
                )

                text = formatting.format_latest_job_result(jobs=jobs)

                return ResponsePlan(
                    system_summary="",
                    ingress_interpretation=ingress_interpretation,
                    direct_text=text,
                    respond_directly=True,
                )

        response_summary = await self.collate_system_information(
            inference_object=inference_object,
            prompt_mode=prompt_mode,
            purpose="response",
            background_job_id=background_job.job_id if background_job else "",
        )

        return ResponsePlan(
            system_summary=response_summary,
            ingress_interpretation=ingress_interpretation,
            background_job=background_job,
        )

    async def _lightweight_interpret(
        self,
        inference_object: InferenceObject,
        prompt_mode: Literal["chat", "terminal"],
        system_summary: str,
    ) -> LightweightIngressInterpretationModel:
        runtime_messages = prompts.build_lightweight_ingress_interpretation_messages(
            messages=inference_object.content,
            prompt_mode=prompt_mode,
            system_information=system_summary,
        )
        effective_tokens = self.primer.status()["effective_n_ctx"]

        reserved_output_tokens = MAX_TOKENS_POLICY.get("inspect", 256)
        temperature_policy = TEMPERATURE_POLICY.get("inspect")
        total_tokens = self.primer._get_total_token_estimation(
            reserved_output_tokens=reserved_output_tokens,
            messages=runtime_messages,
        )
        root_logger.info(f"estimated tokens: {total_tokens}")
        fits = self.primer._can_fit_request(
            effective_n_ctx=effective_tokens,
            reserved_output_tokens=reserved_output_tokens,
            messages=runtime_messages,
        )
        if fits:
            output = await self.primer.chat_json(
                messages=runtime_messages,
                constraints={
                    "temperature": temperature_policy,
                    "stream": False,
                },
                operation="inspect",
            )
            return LIGHTWEIGHT_INGRESS_ADAPTER.validate_python(output)
        else:
            raise ValueError(
                f"Critical Error: Cortex has not enough memory to complete task, tokens requested: {total_tokens}, effective_tokens {effective_tokens}"
            )

    async def collate_system_information(
        self,
        inference_object: InferenceObject,
        prompt_mode: IngressMode,
        *,
        purpose: SystemInformationPurpose,
        terminal_action_result: str | None = None,
        background_job_id: str | None = None,
    ) -> str:
        match purpose:
            case "interpretation":
                return await self._collate_interpretation_information(
                    inference_object=inference_object,
                    prompt_mode=prompt_mode,
                )

            case "response":
                return await self._collate_response_information(
                    inference_object=inference_object,
                    prompt_mode=prompt_mode,
                    terminal_action_result=terminal_action_result,
                    background_job_id=background_job_id,
                )

            case _:
                raise ValueError(f"Unknown system information purpose: {purpose}")

    async def _collate_interpretation_information(
        self,
        inference_object: InferenceObject,
        prompt_mode: IngressMode,
    ) -> str:
        sections: list[str] = []

        sections.append(
            "\n".join(
                [
                    "Ingress context:",
                    f"- mode: {prompt_mode}",
                    f"- user_id: {inference_object.user_id}",
                    f"- session_id: {inference_object.session_id or ''}",
                    f"- channel: {inference_object.channel}",
                ]
            )
        )

        session_jobs = self.job_manager.summarize_jobs_for_prompt(
            session_id=inference_object.session_id,
        )

        if session_jobs:
            sections.append("Session background jobs:\n" + session_jobs)

        commands_information = self.command_router.get_commands(
            mode=prompt_mode,
        )

        root_logger.info(f"commands: {commands_information}")

        if commands_information:
            commands_text = (
                commands_information
                if isinstance(commands_information, str)
                else json.dumps(commands_information, ensure_ascii=False, default=str)
            )

            sections.append("Available mode commands/actions:\n" + commands_text)

        return "\n\n".join(sections)

    async def _collate_response_information(
        self,
        inference_object: InferenceObject,
        prompt_mode: IngressMode,
        *,
        terminal_action_result: str | None = None,
        background_job_id: str | None = None,
    ) -> str:
        sections: list[str] = []

        sections.append(
            "\n".join(
                [
                    "Response context:",
                    f"- mode: {prompt_mode}",
                    f"- user_id: {inference_object.user_id}",
                    f"- session_id: {inference_object.session_id or ''}",
                    f"- channel: {inference_object.channel}",
                ]
            )
        )

        if background_job_id:
            sections.append(
                "\n".join(
                    [
                        "Background job:",
                        f"- job_id: {background_job_id}",
                        "- status: started",
                    ]
                )
            )

        if terminal_action_result:
            sections.append("Terminal action result:\n" + terminal_action_result)

        session_jobs = self.job_manager.summarize_jobs_for_prompt(
            session_id=inference_object.session_id,
        )

        if session_jobs:
            sections.append("Session background jobs:\n" + session_jobs)

        return "\n\n".join(sections)

    def _compact_primer_status(self) -> dict[str, Any]:
        status = self.primer.status()

        return {
            "ready": status.get("ready"),
            "engine": status.get("engine"),
            "model_id": status.get("model_id") or status.get("model"),
            "effective_n_ctx": status.get("effective_n_ctx"),
        }


# ---------------------------------------------------------------------------
# Private Free Functions
# ---------------------------------------------------------------------------


async def _build_work_packets(
    user_id: str, shaped_tasks: list[dict[str, Any]], primer: Primer
) -> list[WorkPacket]:
    packets: list[WorkPacket] = []
    for task in shaped_tasks:
        packets.append(
            await _build_work_packet(user_id=user_id, shaped=task, primer=primer)
        )
    return packets


async def _build_work_packet(
    user_id: str, shaped: dict[str, Any], primer: Primer
) -> WorkPacket:

    work_type = shaped.get("task_type", "llm")
    operation = shaped.get("task_operation", "chat")
    message = shaped.get("task_message", "")
    role = shaped.get("task_role")
    objective = shaped.get("task_objective")
    disposition = (
        WorkDisposition.DEFERRED
        if shaped.get("deferred", False)
        else WorkDisposition.DIRECT
    )
    privacy = shaped.get("privacy_mode")
    context = shaped.get("context_minimum", "")

    messages = build_task_messages(message=message, context=context)
    reserved_tokens = MAX_TOKENS_POLICY.get(operation, 256)
    estimated_tokens = primer._get_total_token_estimation(
        messages=messages,
        reserved_output_tokens=reserved_tokens,
    )
    constraints = {
        "temperature": TEMPERATURE_POLICY.get(operation, 0.1),
        "stream": False,
    }

    ctask = packing.create_canonical_task(
        work_type=work_type,
        operation=operation,
        content=messages,
        constraints=constraints,
        inputs={
            "objective": objective,
            "privacy": privacy,
        },
        routing_hints=RoutingHints(
            role=role,
            required_capabilities=shaped.get("required_capabilities", ["chat"]),
            required_modalities=shaped.get("required_modalities", ["text"]),
            runtime_preference=[{"effective_n_ctx": estimated_tokens}],
        ),
    )

    wpacket = packing.create_workpacket(
        id=f"{user_id}:task",
        disposition=disposition,
        metadata={"origin_stage": "main_ingress", "phase": "executing task"},
        task=ctask,
    )
    return wpacket


def _extract_shaped_task_list(
    value: Any, capability_summary: dict[str, Any]
) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("task_items"), list):
            value = value["task_items"]
        elif isinstance(value.get("items"), list):
            value = value["items"]
        elif isinstance(value.get("tasks"), list):
            value = value["tasks"]
        else:
            raise TypeError(
                "Shaped task JSON object must contain a 'task_items', 'items', or 'tasks' list."
            )

    if not isinstance(value, list):
        raise TypeError(
            f"Shaped task JSON must be a list or object with task_items/items/tasks. "
            f"Got: {type(value).__name__}"
        )

    tasks: list[dict[str, Any]] = []

    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise TypeError(
                f"Shaped task at index {index} must be an object. "
                f"Got: {type(item).__name__}: {item!r}"
            )
        normalized_item = _normalize_task_role(
            item=item, capability_summary=capability_summary
        )

        tasks.append(normalized_item)

    return tasks


def _normalize_task_role(
    item: dict[str, Any],
    capability_summary: dict[str, Any],
) -> dict[str, Any]:
    item = dict(item)

    LLM_OPERATIONS = ["chat", "summarize", "classify", "extract", "analyze"]

    operation = str(item.get("task_operation") or "").strip().lower()
    role = str(item.get("task_role") or "").strip().lower()

    capabilities_by_role = capability_summary.get("capabilities_by_role", {})
    llm_caps = set(capabilities_by_role.get("llm") or [])
    tool_caps = set(capabilities_by_role.get("tool") or [])

    # Background/deferred is not a reason to route to Cortex.
    if role == "cortex" and operation in LLM_OPERATIONS:
        role = "llm"

    if role == "llm":
        required = item.get("required_capabilities") or []

        if not required or required == ["cortex"]:
            if operation in llm_caps:
                required = [operation]
            elif "chat" in llm_caps:
                required = ["chat"]
            else:
                required = []

        item["task_role"] = "llm"
        item["required_capabilities"] = required
        item["required_modalities"] = item.get("required_modalities") or ["text"]

    elif role == "tool":
        required = item.get("required_capabilities") or []

        # Do not silently invent tool capabilities.
        required = [cap for cap in required if cap in tool_caps]

        item["task_role"] = "tool"
        item["required_capabilities"] = required

    else:
        item["task_role"] = role

    item["task_operation"] = operation
    return item


def _output_generator(output_string: str | None):
    output_string = "" if output_string is None else output_string

    chunk_size = 64  # smaller = nicer streaming feel
    for i in range(0, len(output_string), chunk_size):
        yield output_string[i : i + chunk_size]


# ---------------------------------------------------------------------------
# Job Execute Functions
# ---------------------------------------------------------------------------


async def execute_work_packet(job: Job, router: RouterService) -> WorkResult:
    packet = WorkPacket.model_validate(job.spec.payload["packet"])
    result = await router.send(packet=packet)
    if result.status in {"completed", "failed"}:
        return result.sanitize_for_federation()

    return result


async def execute_retrieve_work_packet(
    job: Job,
    router: RouterService,
    response: WorkResult,
) -> WorkResult:
    if response.status in ("completed", "failed"):
        return response

    backend = response.metadata.get("backend_ref")
    work_details: dict[str, Any] = response.metadata.get("work_details", {})

    if not backend:
        return WorkResult(
            status="failed",
            work_id=response.work_id,
            error="No backend_ref found for accepted/running work",
            metadata=response.metadata,
        )

    if isinstance(work_details, dict):
        work_type = work_details.get("work_type", "")
        operation = work_details.get("operation", "")
    else:
        work_type = getattr(work_details, "work_type", "")
        operation = getattr(work_details, "operation", "")

    if not work_type or not operation:
        return WorkResult(
            status="failed",
            work_id=response.work_id,
            error="Missing work_details.work_type or work_details.operation",
            metadata=response.metadata,
        )

    result = await router.retrieve_work(
        work_id=response.work_id,
        work_type=work_type,
        operation=operation,
        backend=backend,
    )

    if result.status in ("completed", "failed"):
        return result

    result.metadata.setdefault("backend_ref", backend)
    result.metadata.setdefault("work_details", work_details)
    return result


async def execute_get_prompt(job: Job, router: RouterService):
    _inference_object = InferenceObject.model_validate(
        job.spec.payload.get("inference_object")
    )
    prompt_context: PromptContextResponse | None = await memory.get_prompts_context(
        router=router, inference_object=_inference_object, limit=6
    )
    _prompt_metadata = prompt_context.model_dump() if prompt_context else {}
    return WorkResult(
        status="completed",
        work_id=f"{_inference_object.user_id}:get_prompt",
        metadata={"prompt_context": _prompt_metadata},
    )


async def execute_interpret_turn(job: Job, router: RouterService, primer: Primer):
    prompt_context = job.spec.payload.get("prompt_context", {})
    inference_object = InferenceObject.model_validate(
        job.spec.payload.get("inference_object")
    )

    runtime_messages = prompts.build_turn_interpretation_messages(
        messages=inference_object.content,
        prompt_context=prompt_context,
    )

    reserved_output_tokens = MAX_TOKENS_POLICY.get("inspect", 256)
    temperature_policy = TEMPERATURE_POLICY.get("inspect")
    total_tokens = primer._get_total_token_estimation(
        reserved_output_tokens=reserved_output_tokens,
        messages=runtime_messages,
    )
    root_logger.info(f"estimated tokens: {total_tokens}")
    #logging.info(f"estimated tokens: {total_tokens}")
    cpacket = packing.create_canonical_task(
        work_type=WorkType.LLM,
        operation="inspect",
        content=runtime_messages,
        inputs={},
        constraints={
            "temperature": temperature_policy,
            "stream": False,
        },
        routing_hints=RoutingHints(
            role=WorkType.LLM,
            required_capabilities=["chat"],
            required_modalities=["text"],
            runtime_preference=[{"effective_n_ctx": total_tokens}],
        ),
    )
    packet = packing.create_workpacket(
        id=f"{inference_object.user_id}:interpret",
        disposition=WorkDisposition.DIRECT,
        metadata={
            "origin_stage": "main_ingress",
            "phase": "interpret",
            "session_id": inference_object.session_id,
            "user_id": inference_object.user_id,
            "channel": inference_object.channel,
        },
        task=cpacket,
    )

    return await router.send(packet=packet)


async def execute_read_interpret(job: Job, router: RouterService, response: WorkResult):

    if response.status in ("completed", "failed"):
        return response

    backend = response.metadata.get("backend_ref", {})
    if not backend:
        return WorkResult(
            status="failed", work_id=response.work_id, error="No Backend found"
        )

    result = await router.retrieve_work(
        work_id=response.work_id,
        work_type=WorkType.LLM,
        operation="inspect",
        backend=backend,
    )

    if result.status in ("completed", "failed"):
        return result

    result.metadata.setdefault("backend_ref", backend)
    return result


async def execute_shape_tasks(job: Job, router: RouterService, primer: Primer):
    inference_object = InferenceObject.model_validate(
        job.spec.payload.get("inference_object")
    )
    _capabilities_summary = router.backend_services.get_capability_summary()

    payload_response = WorkResult.model_validate(job.spec.payload.get("response"))
    _interpreted = unified_types.extract_last_assistant_text(payload_response.content)

    interpreted = json.loads(_interpreted)

    if not interpreted:
        raise ValueError("interpreted is None")

    messages = build_task_shaping_messages(
        interpreted=interpreted,
        capability_summary=_capabilities_summary,
    )

    reserved_output_tokens = MAX_TOKENS_POLICY.get("inspect", 256)
    temperature_policy = TEMPERATURE_POLICY.get("inspect")
    total_tokens = primer._get_total_token_estimation(
        reserved_output_tokens=reserved_output_tokens, messages=messages
    )
    cpacket = packing.create_canonical_task(
        work_type=WorkType.LLM,
        operation="inspect",
        content=messages,
        inputs={},
        constraints={
            "temperature": temperature_policy,
            "stream": False,
        },
        routing_hints=RoutingHints(
            role=WorkType.LLM,
            required_capabilities=["chat"],
            required_modalities=["text"],
            runtime_preference=[{"effective_n_ctx": total_tokens}],
        ),
    )
    packet = packing.create_workpacket(
        id=f"{inference_object.user_id}:shaping",
        disposition=WorkDisposition.DIRECT,
        metadata={
            "origin_stage": "main_ingress",
            "phase": "shaping",
        },
        task=cpacket,
    )

    return await router.send(packet=packet)


async def execute_read_shaping(job: Job, router: RouterService, response: WorkResult):

    if response.status in ("completed", "failed"):
        return response

    backend = response.metadata.get("backend_ref", {})
    if not backend:
        return WorkResult(
            status="failed", work_id=response.work_id, error="No Backend found"
        )

    result = await router.retrieve_work(
        work_id=response.work_id,
        work_type=WorkType.LLM,
        operation="inspect",
        backend=backend,
    )

    if result.status in ("completed", "failed"):
        return result

    result.metadata.setdefault("backend_ref", backend)
    return result


async def execute_build_work_packets(job: Job, router: RouterService, primer: Primer):
    inference_object = InferenceObject.model_validate(
        job.spec.payload.get("inference_object")
    )

    _capabilities_summary = router.backend_services.get_capability_summary()

    payload_result = WorkResult.model_validate(job.spec.payload.get("response"))
    shaped = unified_types.extract_last_assistant_text(payload_result.content)

    root_logger.info(shaped)

    try:
        shaped_raw = json.loads(shaped)
        shaped_tasks = _extract_shaped_task_list(
            shaped_raw, capability_summary=_capabilities_summary
        )
    except json.JSONDecodeError as e:
        return WorkResult(
            status="failed",
            work_id=f"{inference_object.user_id}:build_packets",
            error=e.msg,
        )

    root_logger.info(shaped_tasks)

    work_packets = await _build_work_packets(
        user_id=inference_object.user_id, shaped_tasks=shaped_tasks, primer=primer
    )

    root_logger.info(f"work packets: {work_packets}")

    batch_id = await router.send_many(work_packets)

    return WorkResult(
        status="completed",
        work_id=f"{inference_object.user_id}:build_packets",
        metadata={"batch_id": batch_id},
    )


async def execute_read_and_send_packets(
    job: Job, router: RouterService, job_manager: JobManager
):
    inference_object = InferenceObject.model_validate(
        job.spec.payload.get("inference_object")
    )

    batch_result_adapter = TypeAdapter(list[WorkResult])

    payload_response = WorkResult.model_validate(job.spec.payload.get("response"))

    send_batch_id = payload_response.metadata.get("batch_id", None)

    if not send_batch_id:
        return WorkResult(
            status="failed",
            work_id=f"{inference_object.user_id}:read_and_send_packets",
            error="batch id not found",
        )

    root_logger.info("batch sequence")

    batch_results_raw = await job_manager.batch_progress(batch_id=send_batch_id)

    batch_results = batch_result_adapter.validate_python(batch_results_raw)

    root_logger.info("batch sequence second part")

    retrieve_batch_id = await router.retrieve_many(results=batch_results)

    final_batch_results = await job_manager.batch_progress(batch_id=retrieve_batch_id)

    return WorkResult(
        status="completed",
        work_id=f"{inference_object.user_id}:read_and_send_packets",
        metadata={"packet_results": final_batch_results},
    )


async def execute_read_packet_results(
    job: Job,
):
    inference_object = InferenceObject.model_validate(
        job.spec.payload.get("inference_object")
    )

    batch_result_adapter = TypeAdapter(list[WorkResult])

    payload_response = WorkResult.model_validate(job.spec.payload.get("response"))

    results = payload_response.metadata.get("packet_results", None)

    if results is None:
        return WorkResult(
            status="failed",
            work_id=f"{inference_object.user_id}:read_results",
            error="No result found",
        )

    batch_results = batch_result_adapter.validate_python(results)

    completed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    running: list[dict[str, Any]] = []

    for result in batch_results:
        dumped = result.model_dump(mode="json")

        item = {
            "work_id": dumped.get("work_id"),
            "status": dumped.get("status"),
            "backend_name": dumped.get("backend_name"),
            "backend_model": dumped.get("backend_model"),
            "content": dumped.get("content") or [],
            "error": dumped.get("error"),
            "metadata": dumped.get("metadata") or {},
        }

        if result.status == "completed":
            completed.append(item)
        elif result.status == "failed":
            failed.append(item)
        else:
            running.append(item)

    collated_results = {
        "summary": {
            "total": len(batch_results),
            "completed": len(completed),
            "failed": len(failed),
            "running": len(running),
        },
        "completed": completed,
        "failed": failed,
        "running": running,
    }

    return WorkResult(
        status="completed",
        work_id=f"{inference_object.user_id}:read_results",
        metadata={
            "collated_results": collated_results,
        },
    )


async def execute_final_response(job: Job, router: RouterService, primer: Primer):

    inference_object = InferenceObject.model_validate(
        job.spec.payload.get("inference_object")
    )

    payload_response = WorkResult.model_validate(job.spec.payload.get("response"))

    collated_results = payload_response.metadata.get("collated_results")

    prompt_context_instance: (
        PromptContextResponse | None
    ) = await memory.get_prompts_context(
        router=router, inference_object=inference_object, limit=6
    )

    prompt_context = {}
    if prompt_context_instance:
        prompt_context = prompt_context_instance.model_dump(mode="json")

    messages = build_final_response_messages(
        messages=inference_object.content,
        prompt_context=prompt_context,
        collated_results=collated_results,
    )

    constraints = {
        "temperature": TEMPERATURE_POLICY.get("analyze", 0),
        "stream": False,
    }

    effective_tokens = primer._get_total_token_estimation(
        reserved_output_tokens=MAX_TOKENS_POLICY.get("analyze", 1024),
        messages=messages,
    )

    ctask = packing.create_canonical_task(
        work_type=WorkType.LLM,
        operation="analyze",
        content=messages,
        inputs={},
        constraints=constraints,
        routing_hints=RoutingHints(
            role=WorkType.LLM,
            required_capabilities=["chat"],
            required_modalities=["text"],
            runtime_preference=[{"effective_n_ctx": effective_tokens}],
        ),
    )
    ptask = packing.create_workpacket(
        id=f"{inference_object.user_id}:final:{job.job_id}",
        disposition=WorkDisposition.DIRECT,
        metadata={
            "origin_stage": "main_ingress",
            "phase": "final_response",
        },
        task=ctask,
    )

    return await router.send(packet=ptask)


async def execute_read_final_packet(
    job: Job,
    router: RouterService,
    response: WorkResult,
):
    if response.status in ("completed", "failed"):
        return response

    backend = response.metadata.get("backend_ref", {})
    if not backend:
        return WorkResult(
            status="failed",
            work_id=response.work_id,
            error="No backend found for accepted/running final response packet",
        )

    result = await router.retrieve_work(
        work_id=response.work_id,
        work_type=WorkType.LLM,
        operation="analyze",
        backend=backend,
    )

    if result.status in ("completed", "failed"):
        return result

    result.metadata.setdefault("backend_ref", backend)

    return result
