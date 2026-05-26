from __future__ import annotations

import json
import logging
from typing import Any, Literal, Union, overload

from fastapi.responses import StreamingResponse

from common.factory import packing
from common.jobs import Job, JobManager, JobSpec, PipelineStage
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
from common.protocol.unified_types import PromptContextResponse, RuntimeMessage
from common.types import MAX_TOKENS_POLICY, SAFETY_TOKEN_SIZE, TEMPERATURE_POLICY
from cortex.cli.command_router import CommandRouter
from cortex.cortex import memory
from cortex.cortex import prompts
from cortex.cortex.harness_types import (
    LIGHTWEIGHT_INGRESS_ADAPTER,
    IngressMode,
    LightweightIngressInterpretationModel,
)
from cortex.cortex.prompts import (
    TASK_SHAPING_GRAMMAR,
    TURN_INTERPRETATION_GRAMMAR,
    build_task_messages,
    build_task_shaping_messages,
)
from cortex.router.service import RouterService

SystemInformationMode = Literal["lightweight", "response", "full"]
SystemInformationPurpose = Literal["interpretation", "response"]


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
                    poller=lambda job, result: execute_read_interpret(job, self.router, result),
                    timeout_seconds=90,
                    poll_interval_seconds=1.0,
                ),
                PipelineStage(
                    name="shape_turn",
                    runner=lambda job: execute_shape_tasks(
                        job, self.router, self.primer
                    ),
                    poller=lambda job, result: execute_read_shaping(job, self.router, result),
                    timeout_seconds=180,
                    poll_interval_seconds=1.0,
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
    ) -> Union[StreamingResponse, EgressResponse]:
        _inference_object: InferenceObject = await self._validate_session(req=request)

        match ingression_type:
            case "terminal":
                return await self._response(_inference_object, "terminal")
            case "chat":
                return await self._response(_inference_object, "chat")

    async def handle_work_packet(self, packet: WorkPacket):
        return WorkResult(
            status="failed",
            work_id=packet.work_id,
            error="Harness workpacket handling is not implemented yet.",
        )

    # ---------------------------------------------------------------------------
    # Private Class Function Calls
    # ---------------------------------------------------------------------------

    async def _validate_session(self, req: InferenceSession) -> InferenceObject:
        try:
            _iobj = InferenceObject.model_validate(req.model_dump())

            rsp = await memory.upsert_user(router=self.router, inference_object=_iobj)

            if not rsp:
                logging.info("failed to create/update user")
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
                logging.info(f"Encountered failed responses: {responses}")

            return _iobj
        except Exception as e:
            raise e

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
                (
                    response_system_summary,
                    ingress_interpretation,
                    background_job,
                ) = await self._response_terminal(
                    inference_object=inference_object, prompt_mode="terminal"
                )
            case "chat":
                (
                    response_system_summary,
                    ingress_interpretation,
                    background_job,
                ) = await self._response_chat(
                    inference_object=inference_object, prompt_mode="chat"
                )
            case _:
                raise ValueError("unknown prompt mode")

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
            ingress_interpretation=ingress_interpretation,
            system_information=response_system_summary,
            background_job_id=background_job.job_id if background_job else None,
        )

        reserved_output_tokens = MAX_TOKENS_POLICY.get("chat", 256)
        effective_tokens = _get_total_token_estimation(
            reserved_output_tokens=reserved_output_tokens,
            messages=runtime_messages,
            primer=self.primer,
        )
        logging.info(f"estimated tokens: {effective_tokens}")
        fits = _can_fit_request(
            effective_n_ctx=self.primer.status()["effective_n_ctx"],
            reserved_output_tokens=reserved_output_tokens,
            messages=runtime_messages,
            primer=self.primer,
        )

        if not fits:
            logging.error("Message can not be inferred correctly by backend")

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
        else:
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
        self, inference_object: InferenceObject, prompt_mode: IngressMode
    ):

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

        return (
            await self.collate_system_information(
                inference_object=inference_object,
                prompt_mode=prompt_mode,
                purpose="response",
                terminal_action_result=terminal_action_result,
                background_job_id="",
            ),
            ingress_interpretation,
            None,
        )

    async def _response_chat(
        self, inference_object: InferenceObject, prompt_mode: IngressMode
    ):

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
        terminal_action_result = None
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

        return (
            await self.collate_system_information(
                inference_object=inference_object,
                prompt_mode=prompt_mode,
                purpose="response",
                terminal_action_result=terminal_action_result,
                background_job_id=background_job.job_id if background_job else "",
            ),
            ingress_interpretation,
            background_job,
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
        total_tokens = _get_total_token_estimation(
            reserved_output_tokens=reserved_output_tokens,
            messages=runtime_messages,
            primer=self.primer,
        )
        logging.info(f"estimated tokens: {total_tokens}")
        fits = _can_fit_request(
            effective_n_ctx=effective_tokens,
            reserved_output_tokens=reserved_output_tokens,
            messages=runtime_messages,
            primer=self.primer,
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
# Private Functions
# ---------------------------------------------------------------------------


def _get_total_token_estimation(
    reserved_output_tokens: int, messages: list[RuntimeMessage], primer: Primer
):
    prompt_tokens = primer.count_tokens(messages)
    total_tokens = prompt_tokens + reserved_output_tokens + SAFETY_TOKEN_SIZE
    return total_tokens


def _can_fit_request(
    effective_n_ctx: int,
    reserved_output_tokens: int,
    messages: list[RuntimeMessage],
    primer: Primer,
):
    prompt_tokens = primer.count_tokens(messages)
    total_estimated_nr_ctx = prompt_tokens + reserved_output_tokens + SAFETY_TOKEN_SIZE
    return total_estimated_nr_ctx <= effective_n_ctx

async def _build_work_packets(
        user_id: str, shaped_tasks: list[dict[str, Any]], primer:Primer
    ) -> list[WorkPacket]:
        packets: list[WorkPacket] = []
        for task in shaped_tasks:
            packets.append(
                await _build_work_packet(
                    user_id=user_id,
                    shaped=task,
                    primer=primer
                )
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
        estimated_tokens = _get_total_token_estimation(
            messages=messages,
            reserved_output_tokens=reserved_tokens,
            primer=primer
        )
        constraints = {
            "temperature": TEMPERATURE_POLICY.get(operation, 0.1),
            "stream": False
            }

        ctask = packing.create_canonical_task_messages(
            work_type=work_type,
            operation=operation,
            messages=messages,
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

# ---------------------------------------------------------------------------
# Job Execute Functions
# ---------------------------------------------------------------------------


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
    total_tokens = _get_total_token_estimation(
        reserved_output_tokens=reserved_output_tokens,
        messages=runtime_messages,
        primer=primer,
    )
    logging.info(f"estimated tokens: {total_tokens}")
    cpacket = packing.create_canonical_task_messages(
        work_type=WorkType.LLM,
        operation="inspect",
        messages=runtime_messages,
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

    return await router.execute(packet=packet)


async def execute_read_interpret(job: Job, router: RouterService, response: WorkResult):
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

    result.metadata.setdefault("backend_ref", backend)
    return result


async def execute_shape_tasks(job: Job, router: RouterService, primer: Primer):
    inference_object = InferenceObject.model_validate(
        job.spec.payload.get("inference_object")
    )
    _capabilities_summary = router.backend_services.get_capability_summary()

    payload_response = WorkResult.model_validate(job.spec.payload.get("response"))
    _interpreted = unified_types.extract_last_assistant_text(payload_response.content)

    try:
        interpreted = json.loads(_interpreted)
    except json.JSONDecodeError as e:
        raise e

    if not interpreted:
        raise ValueError("interpreted is None")

    messages = build_task_shaping_messages(
        interpreted=interpreted,
        capability_summary=_capabilities_summary,
    )

    reserved_output_tokens = MAX_TOKENS_POLICY.get("inspect", 256)
    temperature_policy = TEMPERATURE_POLICY.get("inspect")
    total_tokens = _get_total_token_estimation(
        reserved_output_tokens=reserved_output_tokens, messages=messages, primer=primer
    )
    cpacket = packing.create_canonical_task_messages(
        work_type=WorkType.LLM,
        operation="inspect",
        messages=messages,
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

    return await router.execute(packet=packet)


async def execute_read_shaping(job: Job, router: RouterService, response : WorkResult):
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

    if result.status == "completed":
        return result

    result.metadata.setdefault("backend_ref", backend)
    return result


async def execute_build_work_packets(job: Job, router: RouterService, primer:Primer):
    inference_object = InferenceObject.model_validate(
        job.spec.payload.get("inference_object")
    )

    payload_result = WorkResult.model_validate(job.spec.payload.get("response"))
    shaped = unified_types.extract_last_assistant_text(payload_result.content)

    try:
        shaped_tasks = json.loads(shaped)
    except json.JSONDecodeError as e:
        raise e

    work_packets = await _build_work_packets(
        user_id=inference_object.user_id,
        shaped_tasks=shaped_tasks,
        primer=primer
        )
    
    results: list[WorkResult] = []
    for packet in work_packets:
        results.append(await router.execute(packet=packet))

    return WorkResult(status="completed", work_id=f"{inference_object.user_id}:build_packets", metadata={"work_packet_results": results})

async def execute_check_work_packets(job: Job, router: RouterService, primer: Primer):
    pass