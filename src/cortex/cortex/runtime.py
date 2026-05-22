from __future__ import annotations

import logging
from typing import Any, Literal, overload
from fastapi.responses import StreamingResponse
from common.jobs import Job
from common.primer import Primer
from common.protocol import ingress_types
from common.protocol.egress_types import EgressResponse
from common.protocol.ingress_types import InferenceSession, LoadModelRequest
from common.protocol.routing_types import (
    WorkPacket,
    WorkResult,
)

from common.protocol import unified_types

from common.protocol.unified_types import (
    ContentPart,
    RuntimeMessage,
    Visibility,
)
from common.types import SAFETY_TOKEN_SIZE
from cortex.cli.command_router import CommandRouter
from cortex.cluster.discovery.services import DiscoveryService
from cortex.cortex.harness import Harness


class CortexRuntime:
    def __init__(
        self,
        harness: Harness,
        discovery_service: DiscoveryService,
        primer: Primer,
        command_router: CommandRouter,
    ) -> None:
        self.harness = harness
        self.discovery_service = discovery_service
        self.primer = primer
        self.command_router = command_router

    # ---------------------------------------------------------------------------
    # External Network Function Calls
    # ---------------------------------------------------------------------------
    async def handle_session_stream(self, req: InferenceSession):
        if ingress_types.is_terminal_command(req=req):
            message = ingress_types.get_latest_message(req=req)
            if message:
                return await self.command_router.handle_command(message.data)

        if not self.primer.is_ready():
            return self._primer_not_ready_response(stream=True)

        return await self.harness.ingression(
            ingression_type="terminal", request=req, stream=True
        )

    async def handle_session_non_stream(self, req: InferenceSession) -> EgressResponse:
        if ingress_types.is_terminal_command(req=req):
            message = ingress_types.get_latest_message(req=req)
            if message:
                output = await self.command_router.handle_command_text(message.data)
                runtime_message = unified_types.egest_text_runtime_message(
                    role="assistant",
                    content=output,
                    metadata={"visibility": "user", "kind": "command_output"},
                )
                return EgressResponse(
                    content=[runtime_message],
                    metadata={"stream": False, "kind": "terminal"},
                )

        if not self.primer.is_ready():
            return self._primer_not_ready_response(stream=False)

        return await self.harness.ingression(
            ingression_type="terminal", request=req, stream=False
        )

    async def chat_stream(self, req: InferenceSession) -> StreamingResponse:
        if not self.primer.is_ready():
            return self._primer_not_ready_response(stream=True)

        return await self.harness.ingression(
            ingression_type="chat", request=req, stream=True
        )

    async def chat_non_stream(self, req: InferenceSession) -> EgressResponse:
        if not self.primer.is_ready():
            return self._primer_not_ready_response(stream=False)

        return await self.harness.ingression(
            ingression_type="chat", request=req, stream=False
        )

    # ---------------------------------------------------------------------------
    # Internal Network Function Calls
    # ---------------------------------------------------------------------------

    async def handle_ingress(self, req: WorkPacket) -> WorkResult:
        return WorkResult(status="accepted", work_id=req.work_id)

    async def handle_egress(self, work_id: str) -> WorkResult:
        return WorkResult(
            status="failed",
            work_id=work_id,
            content=[],
            error="Cortex deferred not implemented yet",
            backend_model=self.primer.get_model(),
        )

    # ---------------------------------------------------------------------------
    # Private Function Calls
    # ---------------------------------------------------------------------------

    # async def _run_pipeline(self, inf_obj: InferenceObject) -> WorkResult:
    #     prompt_context = await self._get_prompt_context(inference_object=inf_obj)

    #     logging.info(f"context: {prompt_context}")

    #     pending_results = await self._reconcile_session_work(
    #         user_id=inf_obj.user_id,
    #         session_id=inf_obj.session_id,
    #     )

    #     prompt_context = self._inject_work_results_into_prompt_context(
    #         prompt_context=prompt_context,
    #         key="pending_session_work_results",
    #         results=pending_results,
    #     )

    #     logging.info("[Step 2] Prompt Context Retrieved")

    #     logging.info("[Step 3] Interpret User Message")
    #     interpreted = await self._interpret_turn(
    #         inference_object=inf_obj,
    #         prompt_context=prompt_context,
    #     )
    #     logging.info("[Step 3] Interpret Message Done")

    #     explicit_followup_results = await self._resolve_work_result_followups(
    #         interpreted=interpreted,
    #         user_id=inf_obj.user_id,
    #         session_id=inf_obj.session_id,
    #     )

    #     prompt_context = self._inject_work_results_into_prompt_context(
    #         prompt_context=prompt_context,
    #         key="explicit_work_result_followups",
    #         results=explicit_followup_results,
    #     )

    #     _capabilities_summary = self.backend_services.get_capability_summary()
    #     logging.info("[Step 4] Capability Summation Retrieved")

    #     logging.info("[Step 5] Shaping Preliminary Tasks Based on interpretation")
    #     shaped = await self._shape_tasks(
    #         user_id=inf_obj.user_id,
    #         interpreted=interpreted,
    #         capability_summary=_capabilities_summary,
    #     )
    #     logging.info("[Step 5] Task Shaping Completed")

    #     logging.info("[Step 6] Formalizing Selected Tasks and Building WorkPackets")
    #     work_packets = await self._build_work_packets(
    #         user_id=inf_obj.user_id,
    #         shaped_tasks=shaped.get("task_items", []),
    #     )
    #     logging.info("[Step 6] Workpackets Created")

    #     logging.info("[Step 7] Submitting Workpackets To Network")
    #     packet_results = await self._submit_packets(work_packets)
    #     logging.info("[Step 7] Response Packets Retrieved")

    #     packet_results = await self._record_submitted_work(
    #         inf_obj=inf_obj,
    #         packets=work_packets,
    #         results=packet_results,
    #     )

    #     completed_or_failed_results = [
    #         r for r in packet_results if r.status in ("completed", "failed")
    #     ]

    #     accepted_results = [r for r in packet_results if r.status == "accepted"]

    #     visible_results = self._dedupe_work_results(
    #         pending_results + explicit_followup_results + completed_or_failed_results
    #     )

    #     sync_results = [self.to_user_visible_result_block(r) for r in visible_results]

    #     deferred_results = [
    #         self.to_user_visible_result_block(r) for r in accepted_results
    #     ]

    #     clarification_questions = self._extract_clarification_questions(
    #         interpreted=interpreted,
    #         shaped=shaped,
    #     )

    #     logging.info(
    #         "[Step 8] Deferred Results, Sync Results and Clarification Questions Collected"
    #     )

    #     logging.info("[Step 9] Building Final Message and submitting it to the Network")
    #     final_packet = self._build_final_response_packet(
    #         inference_object=inf_obj,
    #         prompt_context=prompt_context,
    #         sync_results=sync_results,
    #         deferred_results=deferred_results,
    #         clarification_questions=clarification_questions,
    #     )

    #     final_response = await self.mailbox.submit(final_packet)

    #     if final_response.status != "accepted":
    #         raise ValueError(
    #             f"Shaping submit expected accepted, got {final_response.status}: "
    #             f"{final_response.error or ''}"
    #         )

    #     backend = final_response.metadata.get("backend_ref")

    #     if not backend:
    #         raise ValueError(
    #             "Shaping work was accepted but no backend_ref was returned. "
    #             f"metadata={final_response.metadata}"
    #         )

    #     final_result = WorkResult(status="running", work_id=final_packet.work_id)

    #     while final_result.status in {"accepted", "running"}:
    #         await asyncio.sleep(3)

    #         final_result = await self.mailbox.retrieve(
    #             packet=final_packet,
    #             backend=backend,
    #         )

    #     if not final_result.status == "completed":
    #         raise ValueError(f"Shaping Failed: {final_result.error or 'unknown error'}")

    #     logging.info("[Step 9] Result returned")

    #     responses = await self._insert_message(
    #         inference_object=inf_obj,
    #         message=final_result.content,
    #     )
    #     logging.info("[Step 9] Saved messages")

    #     if any(response.status == "failed" for response in responses):
    #         logging.info(f"[Step 9] Encountered failed responses: {responses}")

    #     await self._mark_results_surfaced(
    #         user_id=inf_obj.user_id,
    #         session_id=inf_obj.session_id,
    #         results=visible_results,
    #     )

    #     return final_result

    # async def _create_inference_object(self, req: InferenceSession) -> InferenceObject:

    #     inf_obj = InferenceObject.model_validate(req.model_dump())
    #     inf_obj.stream = False  # force stream to be turned off

    #     rsp = await self._upsert_message(inference_object=inf_obj)
    #     # logging.info(f"user:{rsp}")
    #     if not rsp:
    #         logging.info("failed to create user")
    #         raise ValueError("User creation failed")

    #     session_id = inf_obj.session_id
    #     if not session_id:
    #         rsp = await self._resolve_session(inference_object=inf_obj)
    #         # logging.info(f"rsp:{rsp}")
    #         session = rsp.get("session", {})
    #         if session:
    #             session_id = session.get("id")
    #         if not session_id:
    #             raise ValueError("Memory service did not return a session id")
    #         inf_obj.session_id = session_id

    #     responses = await self._insert_message(
    #         inference_object=inf_obj, message=inf_obj.content
    #     )
    #     if any(rsp.status == "failed" for rsp in responses):
    #         logging.info(f"Encountered failed responses: {responses}")

    #     return inf_obj

    # def _is_slash_command(self, inference_object: InferenceObject):

    #     latest_user_message = inference_object.get_latest_message()

    #     logging.info(f"LATEST STUFF {latest_user_message}")

    #     if not latest_user_message:
    #         return False

    #     if latest_user_message.data.startswith("/"):
    #         return True

    #     return False

    def _output_generator(self, output_string: str):
        chunk_size = 64  # smaller = nicer streaming feel
        for i in range(0, len(output_string), chunk_size):
            yield output_string[i : i + chunk_size]

    def _plain_stream(self, output: str, session_id: str = ""):
        return StreamingResponse(
            self._output_generator(output_string=output),
            media_type="text/plain",
            headers={"Cache-Control": "no-cache", "Session-Id": session_id},
        )
    
    @overload
    def _primer_not_ready_response(self, stream: Literal[False]) -> EgressResponse: ...

    @overload
    def _primer_not_ready_response(self, stream: Literal[True]) -> StreamingResponse: ...

    def _primer_not_ready_response(
        self, stream: bool
    ) -> EgressResponse | StreamingResponse:

        content_message = "Primer is not ready"

        if stream:
            return StreamingResponse(
                self._output_generator(content_message),
                media_type="text/plain",
                headers={"Cache-Control": "no-cache"},
            )
        else:
            return EgressResponse(
                content=[
                    RuntimeMessage(
                        role="assistant",
                        parts=[
                            ContentPart(
                                type="text",
                                data=content_message,
                                encoding="plain",
                                mime_type="text/plain",
                            )
                        ],
                        metadata={"visibility": "user"},
                    )
                ],
                metadata={
                    "ok": False,
                    "error": "primer_not_ready",
                    "primer": self.primer.status(),
                },
            )

    # async def _interpret_turn(
    #     self,
    #     *,
    #     inference_object: InferenceObject,
    #     prompt_context: dict[str, Any],
    # ) -> dict[str, Any]:

    #     runtime_messages = build_turn_interpretation_messages(
    #         messages=inference_object.content,
    #         prompt_context=prompt_context,
    #     )

    #     reserved_output_tokens = MAX_TOKENS_POLICY.get("inspect", 256)
    #     temperature_policy = TEMPERATURE_POLICY.get("inspect")
    #     total_tokens = self._get_total_token_estimation(
    #         reserved_output_tokens=reserved_output_tokens,
    #         messages=runtime_messages,
    #     )
    #     logging.info(f"estimated tokens: {total_tokens}")
    #     inferred_runtime_messages = None
    #     fits = self._can_fit_request(
    #         effective_n_ctx=self.primer.status()["effective_n_ctx"],
    #         reserved_output_tokens=reserved_output_tokens,
    #         messages=runtime_messages,
    #     )
    #     if fits:
    #         inferred_runtime_messages = await self.primer.chat_text(
    #             messages=runtime_messages,
    #             constraints={
    #                 "grammar": TURN_INTERPRETATION_GRAMMAR,
    #                 "temperature": temperature_policy,
    #                 "stream": False,
    #             },
    #             operation="inspect",
    #         )
    #     else:
    #         cpacket = packing.create_canonical_task_messages(
    #             work_type=WorkType.LLM,
    #             operation="inspect",
    #             messages=runtime_messages,
    #             inputs={},
    #             constraints={
    #                 "temperature": temperature_policy,
    #                 "stream": False,
    #             },
    #             routing_hints=RoutingHints(
    #                 role=WorkType.LLM,
    #                 required_capabilities=["chat"],
    #                 required_modalities=["text"],
    #                 runtime_preference=[{"effective_n_ctx": total_tokens}],
    #             ),
    #         )
    #         packet = packing.create_workpacket(
    #             id=f"{inference_object.user_id}:interpret",
    #             disposition=WorkDisposition.DIRECT,
    #             metadata={
    #                 "origin_stage": "main_ingress",
    #                 "phase": "interpret",
    #                 "session_id": inference_object.session_id,
    #                 "user_id": inference_object.user_id,
    #                 "channel": inference_object.channel,
    #             },
    #             task=cpacket,
    #         )

    #         response = await self.mailbox.submit(packet)

    #         if response.status != "accepted":
    #             raise ValueError(
    #                 f"Interpretation submit expected accepted, got {response.status}: "
    #                 f"{response.error or ''}"
    #             )

    #         backend = response.metadata.get("backend_ref")

    #         if not backend:
    #             raise ValueError(
    #                 "Interpretation work was accepted but no backend_ref was returned. "
    #                 f"metadata={response.metadata}"
    #             )

    #         result = WorkResult(status="running", work_id=packet.work_id)

    #         while result.status in {"accepted", "running"}:
    #             await asyncio.sleep(3)

    #             result = await self.mailbox.retrieve(
    #                 packet=packet,
    #                 backend=backend,
    #             )

    #         if result.status == "completed":
    #             inferred_runtime_messages = result.content

    #         elif result.status == "failed":
    #             raise ValueError(
    #                 f"Interpretation Failed: {result.error or 'unknown error'}"
    #             )

    #         else:
    #             raise ValueError(
    #                 f"Unexpected interpretation result status: {result.status}"
    #             )

    #     if not inferred_runtime_messages:
    #         raise ValueError("Interpretation Failed")

    #     logging.info(f"runtime messages: {inferred_runtime_messages}")

    #     filtered_messages = self.filter_runtime_messages(
    #         messages=inferred_runtime_messages,
    #         roles=["assistant"],
    #         visibility="user",
    #         kinds=["final"],
    #     )

    #     logging.info(f"filtered messages: {filtered_messages}")

    #     inferred_runtime_message = filtered_messages.pop()

    #     logging.info(f"inferred_runtime_message: {inferred_runtime_message}")

    #     content_parts = self.filter_content_parts(
    #         parts=inferred_runtime_message.parts,
    #         part_types=["text"],
    #         encodings=["plain"],
    #         first_only=True,
    #     )

    #     logging.info(f"content parts: {content_parts}")

    #     if content_parts is None:
    #         raise ValueError("message returned empty")

    #     part = None
    #     if isinstance(content_parts, list):
    #         if 0 < len(content_parts) > 1:
    #             raise ValueError("contentpart is larger than 1 or zero")
    #         part = content_parts.pop()

    #     if isinstance(content_parts, ContentPart):
    #         part = content_parts

    #     if not part:
    #         raise ValueError("Content Part is None")

    #     if isinstance(part.data, str):
    #         try:
    #             result = json.loads(part.data)
    #         except json.JSONDecodeError as e:
    #             raise e
    #     else:
    #         result = part.data

    #     logging.info(f"interpret: {result}")

    #     if isinstance(result, list):
    #         return {"items": result}

    #     if isinstance(result, dict):
    #         if "items" in result and isinstance(result["items"], list):
    #             return result
    #         if "intents" in result and isinstance(result["intents"], list):
    #             return {"items": result["intents"]}

    #     raise ValueError(
    #         f"Unexpected interpretation result shape: {type(result).__name__}"
    #     )

    # async def _shape_tasks(
    #     self,
    #     *,
    #     user_id: str,
    #     interpreted: dict[str, Any],
    #     capability_summary: dict[str, Any],
    # ) -> dict[str, Any]:
    #     messages = build_task_shaping_messages(
    #         interpreted=interpreted,
    #         capability_summary=capability_summary,
    #     )

    #     reserved_output_tokens = MAX_TOKENS_POLICY.get("inspect", 256)
    #     temperature_policy = TEMPERATURE_POLICY.get("inspect")
    #     total_tokens = self._get_total_token_estimation(
    #         reserved_output_tokens=reserved_output_tokens,
    #         messages=messages,
    #     )
    #     fit = self._can_fit_request(
    #         effective_n_ctx=self.primer.status()["effective_n_ctx"],
    #         reserved_output_tokens=reserved_output_tokens,
    #         messages=messages,
    #     )

    #     if fit:
    #         inferred_runtime_messages = await self.primer.chat_text(
    #             messages=messages,
    #             constraints={
    #                 "grammar": TASK_SHAPING_GRAMMAR,
    #                 "temperature": temperature_policy,
    #                 "stream": False,
    #             },
    #             operation="inspect",
    #         )
    #     else:
    #         cpacket = packing.create_canonical_task_messages(
    #             work_type=WorkType.LLM,
    #             operation="inspect",
    #             messages=messages,
    #             inputs={},
    #             constraints={
    #                 "temperature": temperature_policy,
    #                 "stream": False,
    #             },
    #             routing_hints=RoutingHints(
    #                 role=WorkType.LLM,
    #                 required_capabilities=["chat"],
    #                 required_modalities=["text"],
    #                 runtime_preference=[{"effective_n_ctx": total_tokens}],
    #             ),
    #         )
    #         packet = packing.create_workpacket(
    #             id=f"{user_id}:shaping",
    #             disposition=WorkDisposition.DIRECT,
    #             metadata={
    #                 "origin_stage": "main_ingress",
    #                 "phase": "shaping",
    #             },
    #             task=cpacket,
    #         )

    #         response = await self.mailbox.submit(packet)

    #         if response.status != "accepted":
    #             raise ValueError(
    #                 f"Shaping submit expected accepted, got {response.status}: "
    #                 f"{response.error or ''}"
    #             )

    #         backend = response.metadata.get("backend_ref")

    #         if not backend:
    #             raise ValueError(
    #                 "Shaping work was accepted but no backend_ref was returned. "
    #                 f"metadata={response.metadata}"
    #             )

    #         result = WorkResult(status="running", work_id=packet.work_id)

    #         while result.status in {"accepted", "running"}:
    #             await asyncio.sleep(3)

    #             result = await self.mailbox.retrieve(
    #                 packet=packet,
    #                 backend=backend,
    #             )

    #         if result.status == "completed":
    #             inferred_runtime_messages = result.content

    #         elif result.status == "failed":
    #             raise ValueError(f"Shaping Failed: {result.error or 'unknown error'}")

    #         else:
    #             raise ValueError(f"Unexpected Shaping result status: {result.status}")

    #     if not inferred_runtime_messages:
    #         raise ValueError("Shaping Failed")

    #     filtered_messages = self.filter_runtime_messages(
    #         messages=inferred_runtime_messages,
    #         roles=["assistant"],
    #         visibility="user",
    #         kinds=["final"],
    #     )

    #     runtime_message = filtered_messages.pop()
    #     content_parts = self.filter_content_parts(
    #         parts=runtime_message.parts,
    #         part_types=["text"],
    #         encodings=["plain"],
    #         first_only=True,
    #     )

    #     if content_parts is None:
    #         raise ValueError("message returned empty")

    #     part = None
    #     if isinstance(content_parts, list):
    #         if 0 < len(content_parts) > 1:
    #             raise ValueError("contentpart is larger than 1 or zero")
    #         part = content_parts.pop()

    #     if isinstance(content_parts, ContentPart):
    #         part = content_parts

    #     if not part:
    #         raise ValueError("Content Part is None")

    #     if isinstance(part.data, str):
    #         try:
    #             result = json.loads(part.data)
    #         except json.JSONDecodeError as e:
    #             raise e
    #     else:
    #         result = part.data

    #     logging.info(f"shaping: {result}")

    #     if not isinstance(result, dict):
    #         raise ValueError(
    #             f"Task shaping returned unexpected type: {type(result).__name__}"
    #         )

    #     reply_items = result.get("reply_items")
    #     task_items = result.get("task_items")
    #     clarification_items = result.get("clarification_items")

    #     if not isinstance(reply_items, list):
    #         raise ValueError("Task shaping output missing valid 'reply_items'")
    #     if not isinstance(task_items, list):
    #         raise ValueError("Task shaping output missing valid 'task_items'")
    #     if not isinstance(clarification_items, list):
    #         raise ValueError("Task shaping output missing valid 'clarification_items'")

    #     return result

    # async def _build_work_packet(
    #     self, *, user_id: str, shaped: dict[str, Any]
    # ) -> WorkPacket:

    #     work_type = shaped.get("task_type", "llm")
    #     operation = shaped.get("task_operation", "chat")
    #     message = shaped.get("task_message", "")
    #     role = shaped.get("task_role")
    #     objective = shaped.get("task_objective")
    #     disposition = (
    #         WorkDisposition.DEFERRED
    #         if shaped.get("deferred", False)
    #         else WorkDisposition.DIRECT
    #     )
    #     privacy = shaped.get("privacy_mode")
    #     context = shaped.get("context_minimum", "")

    #     messages = build_task_messages(message=message, context=context)
    #     reserved_tokens = MAX_TOKENS_POLICY.get(operation, 256)
    #     estimated_tokens = self._get_total_token_estimation(
    #         messages=messages,
    #         reserved_output_tokens=reserved_tokens,
    #     )
    #     constraints = self._build_task_constraints(
    #         temperature=TEMPERATURE_POLICY.get(operation, 0.1), stream=False
    #     )

    #     ctask = packing.create_canonical_task_messages(
    #         work_type=work_type,
    #         operation=operation,
    #         messages=messages,
    #         constraints=constraints,
    #         inputs={
    #             "objective": objective,
    #             "privacy": privacy,
    #         },
    #         routing_hints=RoutingHints(
    #             role=role,
    #             required_capabilities=shaped.get("required_capabilities", ["chat"]),
    #             required_modalities=shaped.get("required_modalities", ["text"]),
    #             runtime_preference=[{"effective_n_ctx": estimated_tokens}],
    #         ),
    #     )

    #     wpacket = packing.create_workpacket(
    #         id=f"{user_id}:task",
    #         disposition=disposition,
    #         metadata={"origin_stage": "main_ingress", "phase": "executing task"},
    #         task=ctask,
    #     )
    #     return wpacket

    def _build_task_constraints(
        self,
        *,
        temperature: float,
        stream: bool,
    ) -> dict[str, Any]:

        constraints: dict[str, Any] = {
            "temperature": temperature,
            "stream": stream,
        }
        return constraints

    # async def _build_work_packets(
    #     self, *, user_id: str, shaped_tasks: list[dict[str, Any]]
    # ) -> list[WorkPacket]:
    #     packets: list[WorkPacket] = []
    #     for task in shaped_tasks:
    #         packets.append(
    #             await self._build_work_packet(
    #                 user_id=user_id,
    #                 shaped=task,
    #             )
    #         )
    #     return packets

    # async def _submit_packets(self, packets: list[WorkPacket]) -> list[WorkResult]:
    #     results: list[WorkResult] = []
    #     for packet in packets:
    #         results.append(await self.mailbox.submit(packet))
    #     return results

    # async def _retrieve_results(
    #     self,
    #     list_of_deferred: list[tuple[WorkResult, WorkPacket]],
    #     *,
    #     timeout_seconds: float = 180.0,
    #     retry_sleep_seconds: float = 3.0,
    # ) -> list[WorkResult]:
    #     results: list[WorkResult] = []

    #     now = time.monotonic()

    #     for accepted_result, _ in list_of_deferred:
    #         accepted_result.metadata.setdefault("deferred_started_at", now)

    #     pending = list(list_of_deferred)

    #     while pending:
    #         retry_later: list[tuple[WorkResult, WorkPacket]] = []
    #         now = time.monotonic()

    #         for accepted_result, packet in pending:
    #             backend = accepted_result.metadata.get("backend_desc")

    #             if backend is None:
    #                 results.append(
    #                     WorkResult(
    #                         status="failed",
    #                         work_id=packet.work_id,
    #                         backend_name=accepted_result.backend_name,
    #                         backend_model=accepted_result.backend_model,
    #                         error="Deferred work is missing backend descriptor",
    #                         metadata={"phase": "retrieve_results"},
    #                     )
    #                 )
    #                 continue

    #             started_at = float(accepted_result.metadata["deferred_started_at"])
    #             elapsed = now - started_at

    #             if elapsed >= timeout_seconds:
    #                 results.append(
    #                     WorkResult(
    #                         status="failed",
    #                         work_id=packet.work_id,
    #                         backend_name=accepted_result.backend_name,
    #                         backend_model=accepted_result.backend_model,
    #                         error=f"Deferred work timed out after {timeout_seconds:.1f} seconds",
    #                         metadata={
    #                             "phase": "retrieve_results",
    #                             "timeout_seconds": timeout_seconds,
    #                             "elapsed_seconds": elapsed,
    #                         },
    #                     )
    #                 )
    #                 continue

    #             result = await self.mailbox.retrieve(
    #                 packet=packet,
    #                 backend=backend,
    #             )

    #             if result.status == "running":
    #                 retry_later.append((accepted_result, packet))
    #                 continue

    #             if result.status in {"completed", "failed"}:
    #                 results.append(result)
    #                 continue

    #             results.append(
    #                 WorkResult(
    #                     status="failed",
    #                     work_id=packet.work_id,
    #                     backend_name=result.backend_name,
    #                     backend_model=result.backend_model,
    #                     error=f"Unexpected retrieved work status: {result.status}",
    #                     metadata={
    #                         "phase": "retrieve_results",
    #                         "raw_metadata": result.metadata,
    #                     },
    #                 )
    #             )

    #         pending = retry_later

    #         if pending:
    #             await asyncio.sleep(retry_sleep_seconds)

    #     list_of_deferred.clear()

    #     return results

    # def _build_final_response_packet(
    #     self,
    #     *,
    #     inference_object: InferenceObject,
    #     prompt_context: dict[str, Any],
    #     sync_results: list[dict[str, Any]],
    #     deferred_results: list[dict[str, Any]],
    #     clarification_questions: list[str],
    # ) -> WorkPacket:

    #     messages = build_final_response_messages(
    #         messages=inference_object.content,
    #         prompt_context=prompt_context,
    #         sync_results=sync_results,
    #         deferred_results=deferred_results,
    #         clarification_questions=clarification_questions,
    #     )

    #     constraints = self._build_task_constraints(
    #         temperature=TEMPERATURE_POLICY.get("analyze", 0),
    #         stream=inference_object.stream,
    #     )

    #     effective_tokens = self._get_total_token_estimation(
    #         reserved_output_tokens=MAX_TOKENS_POLICY.get("analyze", 1024),
    #         messages=messages,
    #     )

    #     ctask = packing.create_canonical_task_messages(
    #         work_type=WorkType.LLM,
    #         operation="analyze",
    #         messages=messages,
    #         inputs={},
    #         constraints=constraints,
    #         routing_hints=RoutingHints(
    #             role=WorkType.LLM,
    #             required_capabilities=["chat"],
    #             required_modalities=["text"],
    #             runtime_preference=[{"effective_n_ctx": effective_tokens}],
    #         ),
    #     )
    #     ptask = packing.create_workpacket(
    #         id="final",
    #         disposition=WorkDisposition.DIRECT,
    #         metadata={
    #             "origin_stage": "main_ingress",
    #             "phase": "final_response",
    #         },
    #         task=ctask,
    #     )

    #     return ptask

    # def _extract_clarification_questions(
    #     self,
    #     *,
    #     interpreted: dict[str, Any] | list[dict[str, Any]],
    #     shaped: dict[str, Any],
    # ) -> list[str]:
    #     clarification_ids = set(shaped.get("clarification_items", []))
    #     questions: list[str] = []

    #     if isinstance(interpreted, list):
    #         items = interpreted
    #     elif isinstance(interpreted, dict):
    #         items = interpreted.get("items", [])
    #     else:
    #         items = []

    #     for item in items:
    #         if (
    #             isinstance(item, dict)
    #             and item.get("id") in clarification_ids
    #             and item.get("clarification_question")
    #         ):
    #             questions.append(item["clarification_question"])

    #     return questions

    def _can_fit_request(
        self,
        effective_n_ctx: int,
        reserved_output_tokens: int,
        messages: list[RuntimeMessage],
    ):
        prompt_tokens = self.primer.count_tokens(messages)
        total_estimated_nr_ctx = (
            prompt_tokens + reserved_output_tokens + SAFETY_TOKEN_SIZE
        )
        return total_estimated_nr_ctx <= effective_n_ctx

    def _get_total_token_estimation(
        self, reserved_output_tokens: int, messages: list[RuntimeMessage]
    ):
        prompt_tokens = self.primer.count_tokens(messages)
        total_tokens = prompt_tokens + reserved_output_tokens + SAFETY_TOKEN_SIZE
        return total_tokens

    def _runtime_messages_to_text(self, messages: list[RuntimeMessage]) -> str:
        chunks: list[str] = []

        for msg in messages:
            for part in msg.parts:
                if part.type == "text":
                    chunks.append(str(part.data))

                elif part.type == "json":
                    chunks.append(str(part.data))

        return "\n".join(chunk for chunk in chunks if chunk).strip()

    def runtime_messages_to_dicts(
        self,
        messages: list[RuntimeMessage],
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []

        for message in messages:
            if isinstance(message, RuntimeMessage):
                output.append(message.model_dump(mode="json"))
            else:
                raise TypeError(
                    f"Expected RuntimeMessage, got {type(message).__name__}"
                )

        return output

    def dicts_to_runtime_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> list[RuntimeMessage]:
        return [RuntimeMessage.model_validate(message) for message in messages]

    def filter_runtime_messages(
        self,
        *,
        messages: list[RuntimeMessage],
        roles: list[str] | None = None,
        visibility: Visibility | None = None,
        kinds: list[str] | None = None,
    ) -> list[RuntimeMessage]:
        role_set = set(roles) if roles else None
        kind_set = set(kinds) if kinds else None

        filtered: list[RuntimeMessage] = []

        for message in messages:
            if role_set is not None and message.role not in role_set:
                continue

            msg_visibility = message.metadata.get("visibility")
            if visibility is not None and msg_visibility != visibility:
                continue

            msg_kind = message.metadata.get("kind")
            if kind_set is not None and msg_kind not in kind_set:
                continue

            filtered.append(message)

        return filtered

    def filter_content_parts(
        self,
        *,
        parts: list[ContentPart],
        part_types: list[str] | None = None,
        encodings: list[str] | None = None,
        mime_types: list[str] | None = None,
        metadata_filters: dict[str, Any] | None = None,
        first_only: bool = False,
    ) -> list[ContentPart] | ContentPart | None:
        part_type_set = set(part_types) if part_types else None
        encoding_set = set(encodings) if encodings else None
        mime_type_set = set(mime_types) if mime_types else None

        filtered: list[ContentPart] = []

        for part in parts:
            if part_type_set is not None and part.type not in part_type_set:
                continue

            if encoding_set is not None and part.encoding not in encoding_set:
                continue

            if mime_type_set is not None and part.mime_type not in mime_type_set:
                continue

            if metadata_filters is not None:
                matched = True
                for key, value in metadata_filters.items():
                    if part.metadata.get(key) != value:
                        matched = False
                        break
                if not matched:
                    continue

            if first_only:
                return part

            filtered.append(part)

        if first_only:
            return None

        return filtered

    def to_user_visible_result_block(self, result: WorkResult) -> dict[str, Any]:
        visible_messages = self.filter_runtime_messages(
            messages=result.content,
            visibility="user",
        )

        return {
            "work_id": result.work_id,
            "status": result.status,
            "messages": self.runtime_messages_to_dicts(visible_messages),
            "backend_name": result.backend_name,
            "backend_model": result.backend_model,
        }

    async def run_load_model_job(self, job: Job) -> dict[str, Any]:
        req = LoadModelRequest.model_validate(job.spec.payload)

        model_id = req.model_id
        provider = req.provider or "huggingface"
        engine = req.engine
        repo_id = req.repo_id
        filename = req.filename
        revision = req.revision or "main"
        tokenizer_id = req.tokenizer_id
        force_reload = req.force_reload

        if not engine:
            raise ValueError("No engine selected for model load job")

        current_engine = self.primer.status().get("engine")

        if current_engine != engine:
            logging.info(
                "Switching engine: %s -> %s",
                current_engine,
                engine,
            )
            await self.primer.load_engine(engine)

        active_engine = self.primer.status().get("engine")

        if active_engine != engine:
            raise RuntimeError(
                f"Engine switch failed: requested={engine}, active={active_engine}"
            )

        if active_engine == "gguf":
            if not repo_id or not filename:
                raise ValueError("GGUF engine requires repo_id and filename")

            logging.info(
                "Loading GGUF model: model=%s repo=%s file=%s revision=%s",
                model_id,
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
            logging.info(
                "Loading model: model=%s engine=%s provider=%s",
                model_id,
                active_engine,
                provider,
            )

            await self.primer.load_model(
                model_id=model_id,
                provider=provider,
                force_reload=force_reload,
            )

        return {
            "model_id": model_id,
            "engine": active_engine,
            "primer": self.primer.status(),
        }
