from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi.responses import StreamingResponse

from common.factory import packing
from common.primer import Primer
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
    PromptContextRequest,
    ResolveSessionRequest,
    RuntimeMemoryRequest,
    RuntimeMessage,
    User,
    Visibility,
)
from common.types import MAX_TOKENS_POLICY, SAFETY_TOKEN_SIZE, TEMPERATURE_POLICY
from cortex.cli.command_router import CommandRouter
from cortex.cluster.discovery.services import DiscoveryService
from cortex.cortex.mailbox import CortexMailbox
from cortex.cortex.prompts import (
    TASK_SHAPING_GRAMMAR,
    TURN_INTERPRETATION_GRAMMAR,
    build_fast_response_messages,
    build_final_response_messages,
    build_task_messages,
    build_task_shaping_messages,
    build_turn_interpretation_messages,
)


@dataclass
class SessionWorkRef:
    work_id: str
    session_id: str
    user_id: str
    parent_turn_id: str | None
    packet: WorkPacket
    backend: dict[str, Any]
    status: Literal["accepted", "running", "completed", "failed", "expired"]
    created_at: datetime
    updated_at: datetime
    surfaced_at: datetime | None = None
    label: str | None = None


class CortexRuntime:
    def __init__(
        self,
        mailbox: CortexMailbox,
        backend_services: DiscoveryService,
        primer: Primer,
        command_router: CommandRouter,
    ) -> None:
        self.mailbox = mailbox
        self.backend_services = backend_services
        self.primer = primer
        self.command_router = command_router
        self.session_work: dict[str, dict[str, SessionWorkRef]] = {}
        self.session_work_results: dict[str, WorkResult] = {}
        self.session_work_timeout_seconds: int = 15 * 60

    async def handle_session_stream(self, req: InferenceSession):
        logging.info("[Step 1] Creating Inference Object")
        inf_obj = await self._create_inference_object(req=req)

        if self._is_slash_command(inf_obj):
            message = inf_obj.get_latest_message()
            if message:
                if message.data.startswith("/task"):
                    _task_text = message.data.removeprefix("/task").strip()

                    if not _task_text:
                        return self._plain_stream(
                            "Missing task description.\n\nUsage:\n  /task <task description>\n",
                            session_id=inf_obj.session_id if inf_obj.session_id else "",
                        )

                    inf_obj.metadata["background_task"] = {
                        "requested": True,
                        "text": _task_text,
                        "status": "accepted",
                    }
                    asyncio.create_task(self._run_pipeline(inf_obj=inf_obj))
                else:
                    return await self.command_router.handle_command(message.data)

        if not self.primer.is_ready():
            return self._primer_not_ready_response(stream=True)

        runtime_messages = await self._fast_turn(inference_object=inf_obj)

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
                "Session-Id": inf_obj.session_id if inf_obj.session_id else "",
            },
        )

    async def handle_session_non_stream(self, req: InferenceSession):
        logging.info("[Step 1] Creating Inference Object")
        inf_obj = await self._create_inference_object(req=req)

        if self._is_slash_command(inf_obj):
            message = inf_obj.get_latest_message()
            if message:
                if message.data.startswith("/task"):
                    _task_text = message.data.removeprefix("/task").strip()

                    if not _task_text:
                        return EgressResponse(
                            content=[
                                RuntimeMessage(
                                    role="assistant",
                                    parts=[
                                        ContentPart(
                                            type="text",
                                            data="Missing task description.\n\nUsage:\n  /task <task description>\n",
                                            encoding="plain",
                                            mime_type="text/plain",
                                        )
                                    ],
                                    metadata={
                                        "visibility": "user",
                                        "kind": "command_output",
                                    },
                                )
                            ],
                            session_id=inf_obj.session_id,
                            metadata={"stream": False, "kind": "terminal"},
                        )

                    inf_obj.metadata["background_task"] = {
                        "requested": True,
                        "text": _task_text,
                        "status": "accepted",
                    }
                    asyncio.create_task(self._run_pipeline(inf_obj=inf_obj))
                else:
                    output = await self.command_router.handle_command_text(message.data)
                    return EgressResponse(
                        content=[
                            RuntimeMessage(
                                role="assistant",
                                parts=[
                                    ContentPart(
                                        type="text",
                                        data=output,
                                        encoding="plain",
                                        mime_type="text/plain",
                                    )
                                ],
                                metadata={
                                    "visibility": "user",
                                    "kind": "command_output",
                                },
                            )
                        ],
                        session_id=inf_obj.session_id,
                        metadata={"stream": False, "kind": "terminal"},
                    )

        if not self.primer.is_ready():
            return self._primer_not_ready_response()

        runtime_messages = await self._fast_turn(inference_object=inf_obj)

        response_messages = await self.primer.chat_text(
            messages=runtime_messages,
            constraints={"temperature": TEMPERATURE_POLICY.get("chat")},
            operation="chat",
        )

        return EgressResponse(
            content=response_messages,
            session_id=inf_obj.session_id,
            metadata={
                "stream": False,
                "mode": "fast_response",
            },
        )

    async def chat_stream(self, req: InferenceSession):
        logging.info("[Step 1] Creating Inference Object")
        inf_obj = await self._create_inference_object(req=req)

        if not inf_obj.session_id:
            return self._plain_stream(
                "Missing Session ID",
                session_id="",
            )

        result = await self._run_pipeline(inf_obj=inf_obj)

        return self._plain_stream(
            output=self._runtime_messages_to_text(result.content),
            session_id=inf_obj.session_id,
        )

    async def chat_non_stream(self, req: InferenceSession) -> EgressResponse:
        logging.info("[Step 1] Creating Inference Object")
        inf_obj = await self._create_inference_object(req=req)

        result = await self._run_pipeline(inf_obj=inf_obj)

        return EgressResponse(
            content=result.content,
            work_id=result.work_id,
            session_id=inf_obj.session_id,
            metadata={
                "kind": "chat",
                "stream": False,
            },
        )

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

    async def _run_pipeline(self, inf_obj: InferenceObject) -> WorkResult:
        prompt_context = await self._get_prompt_context(inference_object=inf_obj)

        logging.info(f"context: {prompt_context}")

        pending_results = await self._reconcile_session_work(
            user_id=inf_obj.user_id,
            session_id=inf_obj.session_id,
        )

        prompt_context = self._inject_work_results_into_prompt_context(
            prompt_context=prompt_context,
            key="pending_session_work_results",
            results=pending_results,
        )

        logging.info("[Step 2] Prompt Context Retrieved")

        logging.info("[Step 3] Interpret User Message")
        interpreted = await self._interpret_turn(
            inference_object=inf_obj,
            prompt_context=prompt_context,
        )
        logging.info("[Step 3] Interpret Message Done")

        explicit_followup_results = await self._resolve_work_result_followups(
            interpreted=interpreted,
            user_id=inf_obj.user_id,
            session_id=inf_obj.session_id,
        )

        prompt_context = self._inject_work_results_into_prompt_context(
            prompt_context=prompt_context,
            key="explicit_work_result_followups",
            results=explicit_followup_results,
        )

        _capabilities_summary = self.backend_services.get_capability_summary()
        logging.info("[Step 4] Capability Summation Retrieved")

        logging.info("[Step 5] Shaping Preliminary Tasks Based on interpretation")
        shaped = await self._shape_tasks(
            user_id=inf_obj.user_id,
            interpreted=interpreted,
            capability_summary=_capabilities_summary,
        )
        logging.info("[Step 5] Task Shaping Completed")

        logging.info("[Step 6] Formalizing Selected Tasks and Building WorkPackets")
        work_packets = await self._build_work_packets(
            user_id=inf_obj.user_id,
            shaped_tasks=shaped.get("task_items", []),
        )
        logging.info("[Step 6] Workpackets Created")

        logging.info("[Step 7] Submitting Workpackets To Network")
        packet_results = await self._submit_packets(work_packets)
        logging.info("[Step 7] Response Packets Retrieved")

        packet_results = await self._record_submitted_work(
            inf_obj=inf_obj,
            packets=work_packets,
            results=packet_results,
        )

        completed_or_failed_results = [
            r for r in packet_results if r.status in ("completed", "failed")
        ]

        accepted_results = [r for r in packet_results if r.status == "accepted"]

        visible_results = self._dedupe_work_results(
            pending_results + explicit_followup_results + completed_or_failed_results
        )

        sync_results = [self.to_user_visible_result_block(r) for r in visible_results]

        deferred_results = [
            self.to_user_visible_result_block(r) for r in accepted_results
        ]

        clarification_questions = self._extract_clarification_questions(
            interpreted=interpreted,
            shaped=shaped,
        )

        logging.info(
            "[Step 8] Deferred Results, Sync Results and Clarification Questions Collected"
        )

        logging.info("[Step 9] Building Final Message and submitting it to the Network")
        final_packet = self._build_final_response_packet(
            inference_object=inf_obj,
            prompt_context=prompt_context,
            sync_results=sync_results,
            deferred_results=deferred_results,
            clarification_questions=clarification_questions,
        )

        final_response = await self.mailbox.submit(final_packet)

        if final_response.status != "accepted":
            raise ValueError(
                f"Shaping submit expected accepted, got {final_response.status}: "
                f"{final_response.error or ''}"
            )

        backend = final_response.metadata.get("backend_ref")

        if not backend:
            raise ValueError(
                "Shaping work was accepted but no backend_ref was returned. "
                f"metadata={final_response.metadata}"
            )

        final_result = WorkResult(status="running", work_id=final_packet.work_id)

        while final_result.status in {"accepted", "running"}:
            await asyncio.sleep(3)

            final_result = await self.mailbox.retrieve(
                packet=final_packet,
                backend=backend,
            )

        if not final_result.status == "completed":
            raise ValueError(f"Shaping Failed: {final_result.error or 'unknown error'}")

        logging.info("[Step 9] Result returned")

        responses = await self._insert_message(
            inference_object=inf_obj,
            message=final_result.content,
        )
        logging.info("[Step 9] Saved messages")

        if any(response.status == "failed" for response in responses):
            logging.info(f"[Step 9] Encountered failed responses: {responses}")

        await self._mark_results_surfaced(
            user_id=inf_obj.user_id,
            session_id=inf_obj.session_id,
            results=visible_results,
        )

        return final_result

    async def _create_inference_object(self, req: InferenceSession) -> InferenceObject:

        inf_obj = InferenceObject.model_validate(req.model_dump())
        inf_obj.stream = False  # force stream to be turned off

        rsp = await self._upsert_message(inference_object=inf_obj)
        # logging.info(f"user:{rsp}")
        if not rsp:
            logging.info("failed to create user")
            raise ValueError("User creation failed")

        session_id = inf_obj.session_id
        if not session_id:
            rsp = await self._resolve_session(inference_object=inf_obj)
            # logging.info(f"rsp:{rsp}")
            session = rsp.get("session", {})
            if session:
                session_id = session.get("id")
            if not session_id:
                raise ValueError("Memory service did not return a session id")
            inf_obj.session_id = session_id

        responses = await self._insert_message(
            inference_object=inf_obj, message=inf_obj.content
        )
        if any(rsp.status == "failed" for rsp in responses):
            logging.info(f"Encountered failed responses: {responses}")

        return inf_obj

    def _is_slash_command(self, inference_object: InferenceObject):

        latest_user_message = inference_object.get_latest_message()

        logging.info(f"LATEST STUFF {latest_user_message}")

        if not latest_user_message:
            return False

        if latest_user_message.data.startswith("/"):
            return True

        return False

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

    def _primer_not_ready_response(
        self, stream: bool = False
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

    def _utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def _format_work_results_for_prompt(
        self,
        results: list[WorkResult],
    ) -> list[dict[str, Any]]:
        formatted: list[dict[str, Any]] = []

        for result in results:
            formatted.append(
                {
                    "work_id": result.work_id,
                    "status": result.status,
                    "backend_name": result.backend_name,
                    "backend_model": result.backend_model,
                    "error": result.error,
                    "content": self._runtime_messages_to_text(result.content),
                    "metadata": result.metadata,
                }
            )

        return formatted

    def _inject_work_results_into_prompt_context(
        self,
        *,
        prompt_context: dict[str, Any] | None,
        key: str,
        results: list[WorkResult],
    ) -> dict[str, Any]:
        context = dict(prompt_context or {})

        if not results:
            return context

        existing = context.get(key)

        if isinstance(existing, list):
            merged = existing + self._format_work_results_for_prompt(results)
        else:
            merged = self._format_work_results_for_prompt(results)

        context[key] = merged
        return context

    async def _resolve_work_result_followups(
        self,
        *,
        interpreted: dict[str, Any],
        user_id: str,
        session_id: str | None,
    ) -> list[WorkResult]:
        if not session_id:
            return []

        items = interpreted.get("items", [])

        wants_followup = any(
            item.get("kind") == "work_result_followup" for item in items
        )

        if not wants_followup:
            return []

        session_refs = self.session_work.get(session_id)
        if not session_refs:
            return []

        refs = [ref for ref in session_refs.values() if ref.user_id == user_id]

        refs.sort(
            key=lambda ref: ref.updated_at,
            reverse=True,
        )

        results: list[WorkResult] = []

        for ref in refs:
            if ref.status in ("accepted", "running"):
                if self._session_work_ref_expired(ref):
                    result = self._make_expired_work_result(ref)
                else:
                    result = await self._retrieve_session_work_ref(ref)

                self._update_session_work_ref_from_result(ref, result)
                results.append(result)
                continue

            result = self.session_work_results.get(ref.work_id)
            if result is not None:
                results.append(result)

        return self._dedupe_work_results(results)

    async def _mark_results_surfaced(
        self,
        *,
        user_id: str,
        session_id: str | None,
        results: list[WorkResult],
    ) -> None:
        if not session_id:
            return

        session_refs = self.session_work.get(session_id)
        if not session_refs:
            return

        now = self._utcnow()

        for result in results:
            if result.status not in ("completed", "failed"):
                continue

            ref = session_refs.get(result.work_id)
            if ref is None:
                continue

            if ref.user_id != user_id:
                continue

            ref.surfaced_at = now
            ref.updated_at = now

    async def _reconcile_session_work(
        self,
        *,
        user_id: str,
        session_id: str | None,
    ) -> list[WorkResult]:
        if not session_id:
            return []

        session_refs = self.session_work.get(session_id)
        if not session_refs:
            return []

        visible_results: list[WorkResult] = []

        for ref in list(session_refs.values()):
            if ref.user_id != user_id:
                continue

            if ref.surfaced_at is not None:
                continue

            if ref.status in ("completed", "failed", "expired"):
                result = self.session_work_results.get(ref.work_id)

                if result is not None and result.status in ("completed", "failed"):
                    visible_results.append(result)

                continue

            if ref.status not in ("accepted", "running"):
                continue

            if self._session_work_ref_expired(ref):
                result = self._make_expired_work_result(ref)
                self._update_session_work_ref_from_result(ref, result)
                visible_results.append(result)
                continue

            result = await self._retrieve_session_work_ref(ref)
            self._update_session_work_ref_from_result(ref, result)

            if result.status in ("completed", "failed"):
                visible_results.append(result)

        return self._dedupe_work_results(visible_results)

    async def _record_submitted_work(
        self,
        *,
        inf_obj: InferenceObject,
        packets: list[WorkPacket],
        results: list[WorkResult],
    ) -> list[WorkResult]:
        if not inf_obj.session_id:
            return results

        session_refs = self.session_work.setdefault(inf_obj.session_id, {})
        normalized_results: list[WorkResult] = []

        now = self._utcnow()

        for packet, result in zip(packets, results):
            if result.status != "accepted":
                normalized_results.append(result)
                continue

            backend = self._backend_desc_from_result(result)

            if backend is None:
                failed = self._make_missing_backend_desc_result(
                    packet=packet,
                    result=result,
                )
                self.session_work_results[failed.work_id] = failed
                normalized_results.append(failed)
                continue

            ref = SessionWorkRef(
                work_id=packet.work_id,
                session_id=inf_obj.session_id,
                user_id=inf_obj.user_id,
                parent_turn_id=getattr(inf_obj, "turn_id", None),
                packet=packet,
                backend=backend,
                status="accepted",
                created_at=now,
                updated_at=now,
                surfaced_at=None,
                label=result.metadata.get("label"),
            )

            session_refs[packet.work_id] = ref
            self.session_work_results[packet.work_id] = result
            normalized_results.append(result)

        return normalized_results

    def _session_work_ref_expired(self, ref: SessionWorkRef) -> bool:
        age_seconds = (self._utcnow() - ref.created_at).total_seconds()
        return age_seconds >= self.session_work_timeout_seconds

    def _backend_desc_from_result(
        self,
        result: WorkResult,
    ) -> dict | None:
        backend = result.metadata.get("backend_desc")

        if isinstance(backend, dict):
            return backend

        return None

    def _make_missing_backend_desc_result(
        self,
        *,
        packet: WorkPacket,
        result: WorkResult,
    ) -> WorkResult:
        return WorkResult(
            status="failed",
            work_id=packet.work_id,
            content=[
                RuntimeMessage(
                    role="assistant",
                    parts=[
                        ContentPart(
                            type="text",
                            data=(
                                "The work was accepted, but Cortex did not receive "
                                "a backend descriptor for later retrieval."
                            ),
                            encoding="plain",
                        )
                    ],
                    metadata={
                        "kind": "work_result_status",
                        "visibility": "internal",
                    },
                )
            ],
            backend_name=result.backend_name,
            backend_model=result.backend_model,
            error="missing_backend_descriptor",
            metadata={
                "kind": "work_result",
                "reason": "missing_backend_descriptor",
                "original_status": result.status,
                "original_metadata": result.metadata,
            },
        )

    def _make_expired_work_result(
        self,
        ref: SessionWorkRef,
    ) -> WorkResult:
        return WorkResult(
            status="failed",
            work_id=ref.work_id,
            content=[
                RuntimeMessage(
                    role="assistant",
                    parts=[
                        ContentPart(
                            type="text",
                            data="The deferred work did not complete before the timeout.",
                            encoding="plain",
                        )
                    ],
                    metadata={
                        "kind": "work_result_status",
                        "visibility": "internal",
                    },
                )
            ],
            backend_name=None,
            backend_model=None,
            error="deferred_work_timeout",
            metadata={
                "kind": "work_result",
                "session_id": ref.session_id,
                "label": ref.label,
                "reason": "timeout",
            },
        )

    async def _retrieve_session_work_ref(
        self,
        ref: SessionWorkRef,
    ) -> WorkResult:
        try:
            return await self.mailbox.retrieve(
                packet=ref.packet,
                backend=ref.backend,
            )

        except Exception as exc:
            return self._make_retrieval_failed_work_result(
                ref=ref,
                error=exc,
            )

    def _make_retrieval_failed_work_result(
        self,
        *,
        ref: SessionWorkRef,
        error: Exception,
    ) -> WorkResult:
        return WorkResult(
            status="failed",
            work_id=ref.work_id,
            content=[
                RuntimeMessage(
                    role="assistant",
                    parts=[
                        ContentPart(
                            type="text",
                            data="The deferred work could not be retrieved.",
                            encoding="plain",
                        )
                    ],
                    metadata={
                        "kind": "work_result_status",
                        "visibility": "internal",
                    },
                )
            ],
            backend_name=None,
            backend_model=None,
            error=str(error),
            metadata={
                "kind": "work_result",
                "session_id": ref.session_id,
                "label": ref.label,
                "reason": "retrieval_failed",
            },
        )

    def _dedupe_work_results(
        self,
        results: list[WorkResult],
    ) -> list[WorkResult]:
        seen: set[str] = set()
        deduped: list[WorkResult] = []

        for result in results:
            if result.work_id in seen:
                continue

            seen.add(result.work_id)
            deduped.append(result)

        return deduped

    def _update_session_work_ref_from_result(
        self,
        ref: SessionWorkRef,
        result: WorkResult,
    ) -> None:
        if result.error == "deferred_work_timeout":
            ref.status = "expired"
        elif result.status in ("accepted", "running", "completed", "failed"):
            ref.status = result.status
        else:
            ref.status = "failed"

        ref.updated_at = self._utcnow()
        self.session_work_results[result.work_id] = result

    def _assistant_text_message(self, text: str) -> RuntimeMessage:
        return RuntimeMessage(
            role="assistant",
            parts=[
                ContentPart(
                    type="text",
                    data=text,
                    encoding="plain",
                )
            ],
            metadata={
                "kind": "work_result_status",
                "visibility": "internal",
            },
        )

    async def _upsert_message(
        self, *, inference_object: InferenceObject
    ) -> dict[str, Any]:
        runtime_message_request = RuntimeMemoryRequest(
            user_id=inference_object.user_id,
            session_id=inference_object.session_id,
            channel=inference_object.channel,
            request=User(
                external_id=inference_object.user_id,
            ),
        )

        cpacket = packing.create_canonical_task_memory(
            work_type=WorkType.MEMORY,
            operation="upsert_user",
            memory_request=runtime_message_request,
            inputs={},
            constraints={},
            routing_hints=RoutingHints(role=WorkType.MEMORY),
        )

        packet = packing.create_workpacket(
            id=f"{inference_object.user_id}:upsert_user",
            disposition=WorkDisposition.DIRECT,
            metadata={},
            task=cpacket,
        )

        response = await self.mailbox.submit(packet=packet)

        if response.status == "completed":
            return response.metadata
        else:
            return {}

    async def _resolve_session(
        self, *, inference_object: InferenceObject
    ) -> dict[str, Any]:
        runtime_message_request = RuntimeMemoryRequest(
            user_id=inference_object.user_id,
            session_id=inference_object.session_id,
            channel=inference_object.channel,
            request=ResolveSessionRequest(max_idle_minutes=60),
        )

        cpacket = packing.create_canonical_task_memory(
            work_type=WorkType.MEMORY,
            operation="resolve_session",
            memory_request=runtime_message_request,
            inputs={},
            constraints={},
            routing_hints=RoutingHints(role=WorkType.MEMORY),
        )

        packet = packing.create_workpacket(
            id=f"{inference_object.user_id}:resolve_session",
            disposition=WorkDisposition.DIRECT,
            metadata={},
            task=cpacket,
        )

        response = await self.mailbox.submit(packet=packet)

        if response.status == "completed":
            return response.metadata
        else:
            return {}

    async def _get_prompt_context(
        self,
        *,
        inference_object: InferenceObject,
        limit: int = 6,
        recent_message_roles: list[str] = ["user", "assistant"],
    ) -> dict[str, Any]:

        runtime_message_request = RuntimeMemoryRequest(
            user_id=inference_object.user_id,
            session_id=inference_object.session_id,
            channel=inference_object.channel,
            request=PromptContextRequest(
                query="",
                include_recent_messages=True,
                recent_message_limit=limit,
                recent_message_roles=recent_message_roles,
            ),
        )

        cpacket = packing.create_canonical_task_memory(
            work_type=WorkType.MEMORY,
            operation="prompt_context",
            memory_request=runtime_message_request,
            inputs={},
            constraints={},
            routing_hints=RoutingHints(role=WorkType.MEMORY),
        )

        packet = packing.create_workpacket(
            id=f"{inference_object.user_id}:prompt_context",
            disposition=WorkDisposition.DIRECT,
            metadata={},
            task=cpacket,
        )

        response = await self.mailbox.submit(packet=packet)

        if response.status == "completed":
            return response.metadata
        else:
            return {}

    async def _insert_message(
        self, *, inference_object: InferenceObject, message: list[RuntimeMessage]
    ) -> list[WorkResult]:

        responses = []
        for msg in message:
            runtime_message_request = RuntimeMemoryRequest(
                user_id=inference_object.user_id,
                session_id=inference_object.session_id,
                channel=inference_object.channel,
                request=msg,
            )

            cpacket = packing.create_canonical_task_memory(
                work_type=WorkType.MEMORY,
                operation="create_message",
                memory_request=runtime_message_request,
                inputs={},
                constraints={},
                routing_hints=RoutingHints(role=WorkType.MEMORY),
            )

            packet = packing.create_workpacket(
                id=f"{inference_object.user_id}:create_message",
                disposition=WorkDisposition.DIRECT,
                metadata={},
                task=cpacket,
            )

            response = await self.mailbox.submit(packet=packet)

            responses.append(response)

        return responses

    async def _fast_turn(self, *, inference_object: InferenceObject):
        task = inference_object.metadata.get("background_task")

        last_assistant_message = await self._get_last_assistant_message(
            inference_object=inference_object
        )

        runtime_messages = build_fast_response_messages(
            messages=inference_object.content,
            last_assistant_message=last_assistant_message,
            background_task=task,
        )

        reserved_output_tokens = MAX_TOKENS_POLICY.get("chat", 256)
        effective_tokens = self._get_total_token_estimation(
            reserved_output_tokens=reserved_output_tokens,
            messages=runtime_messages,
        )
        logging.info(f"estimated tokens: {effective_tokens}")
        fits = self._can_fit_request(
            effective_n_ctx=self.primer.status()["effective_n_ctx"],
            reserved_output_tokens=reserved_output_tokens,
            messages=runtime_messages,
        )
        if not fits:
            logging.error("Message can not be inferred correctly by backend")
        return runtime_messages

    async def _get_last_assistant_message(
        self,
        *,
        inference_object: InferenceObject,
    ) -> RuntimeMessage | None:
        prompt_context = await self._get_prompt_context(
            inference_object=inference_object,
            limit=1,
            recent_message_roles=["assistant"],
        )

        messages = prompt_context.get("recent_messages", [])

        if not messages:
            return None

        msg = messages[-1]

        if isinstance(msg, RuntimeMessage):
            return msg

        if isinstance(msg, dict):
            return RuntimeMessage.model_validate(msg)

        if isinstance(msg, str):
            return RuntimeMessage(
                role="assistant",
                parts=[
                    ContentPart(
                        type="text",
                        data=msg,
                        encoding="plain",
                        mime_type="text/plain",
                    )
                ],
            )

        return None

    async def _interpret_turn(
        self,
        *,
        inference_object: InferenceObject,
        prompt_context: dict[str, Any],
    ) -> dict[str, Any]:

        runtime_messages = build_turn_interpretation_messages(
            messages=inference_object.content,
            prompt_context=prompt_context,
        )

        reserved_output_tokens = MAX_TOKENS_POLICY.get("inspect", 256)
        temperature_policy = TEMPERATURE_POLICY.get("inspect")
        total_tokens = self._get_total_token_estimation(
            reserved_output_tokens=reserved_output_tokens,
            messages=runtime_messages,
        )
        logging.info(f"estimated tokens: {total_tokens}")
        inferred_runtime_messages = None
        fits = self._can_fit_request(
            effective_n_ctx=self.primer.status()["effective_n_ctx"],
            reserved_output_tokens=reserved_output_tokens,
            messages=runtime_messages,
        )
        if fits:
            inferred_runtime_messages = await self.primer.chat_text(
                messages=runtime_messages,
                constraints={
                    "grammar": TURN_INTERPRETATION_GRAMMAR,
                    "temperature": temperature_policy,
                    "stream": False,
                },
                operation="inspect",
            )
        else:
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

            response = await self.mailbox.submit(packet)

            if response.status != "accepted":
                raise ValueError(
                    f"Interpretation submit expected accepted, got {response.status}: "
                    f"{response.error or ''}"
                )

            backend = response.metadata.get("backend_ref")

            if not backend:
                raise ValueError(
                    "Interpretation work was accepted but no backend_ref was returned. "
                    f"metadata={response.metadata}"
                )

            result = WorkResult(status="running", work_id=packet.work_id)

            while result.status in {"accepted", "running"}:
                await asyncio.sleep(3)

                result = await self.mailbox.retrieve(
                    packet=packet,
                    backend=backend,
                )

            if result.status == "completed":
                inferred_runtime_messages = result.content

            elif result.status == "failed":
                raise ValueError(
                    f"Interpretation Failed: {result.error or 'unknown error'}"
                )

            else:
                raise ValueError(
                    f"Unexpected interpretation result status: {result.status}"
                )

        if not inferred_runtime_messages:
            raise ValueError("Interpretation Failed")

        logging.info(f"runtime messages: {inferred_runtime_messages}")

        filtered_messages = self.filter_runtime_messages(
            messages=inferred_runtime_messages,
            roles=["assistant"],
            visibility="user",
            kinds=["final"],
        )

        logging.info(f"filtered messages: {filtered_messages}")

        inferred_runtime_message = filtered_messages.pop()

        logging.info(f"inferred_runtime_message: {inferred_runtime_message}")

        content_parts = self.filter_content_parts(
            parts=inferred_runtime_message.parts,
            part_types=["text"],
            encodings=["plain"],
            first_only=True,
        )

        logging.info(f"content parts: {content_parts}")

        if content_parts is None:
            raise ValueError("message returned empty")

        part = None
        if isinstance(content_parts, list):
            if 0 < len(content_parts) > 1:
                raise ValueError("contentpart is larger than 1 or zero")
            part = content_parts.pop()

        if isinstance(content_parts, ContentPart):
            part = content_parts

        if not part:
            raise ValueError("Content Part is None")

        if isinstance(part.data, str):
            try:
                result = json.loads(part.data)
            except json.JSONDecodeError as e:
                raise e
        else:
            result = part.data

        logging.info(f"interpret: {result}")

        if isinstance(result, list):
            return {"items": result}

        if isinstance(result, dict):
            if "items" in result and isinstance(result["items"], list):
                return result
            if "intents" in result and isinstance(result["intents"], list):
                return {"items": result["intents"]}

        raise ValueError(
            f"Unexpected interpretation result shape: {type(result).__name__}"
        )

    async def _shape_tasks(
        self,
        *,
        user_id: str,
        interpreted: dict[str, Any],
        capability_summary: dict[str, Any],
    ) -> dict[str, Any]:
        messages = build_task_shaping_messages(
            interpreted=interpreted,
            capability_summary=capability_summary,
        )

        reserved_output_tokens = MAX_TOKENS_POLICY.get("inspect", 256)
        temperature_policy = TEMPERATURE_POLICY.get("inspect")
        total_tokens = self._get_total_token_estimation(
            reserved_output_tokens=reserved_output_tokens,
            messages=messages,
        )
        fit = self._can_fit_request(
            effective_n_ctx=self.primer.status()["effective_n_ctx"],
            reserved_output_tokens=reserved_output_tokens,
            messages=messages,
        )

        if fit:
            inferred_runtime_messages = await self.primer.chat_text(
                messages=messages,
                constraints={
                    "grammar": TASK_SHAPING_GRAMMAR,
                    "temperature": temperature_policy,
                    "stream": False,
                },
                operation="inspect",
            )
        else:
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
                id=f"{user_id}:shaping",
                disposition=WorkDisposition.DIRECT,
                metadata={
                    "origin_stage": "main_ingress",
                    "phase": "shaping",
                },
                task=cpacket,
            )

            response = await self.mailbox.submit(packet)

            if response.status != "accepted":
                raise ValueError(
                    f"Shaping submit expected accepted, got {response.status}: "
                    f"{response.error or ''}"
                )

            backend = response.metadata.get("backend_ref")

            if not backend:
                raise ValueError(
                    "Shaping work was accepted but no backend_ref was returned. "
                    f"metadata={response.metadata}"
                )

            result = WorkResult(status="running", work_id=packet.work_id)

            while result.status in {"accepted", "running"}:
                await asyncio.sleep(3)

                result = await self.mailbox.retrieve(
                    packet=packet,
                    backend=backend,
                )

            if result.status == "completed":
                inferred_runtime_messages = result.content

            elif result.status == "failed":
                raise ValueError(f"Shaping Failed: {result.error or 'unknown error'}")

            else:
                raise ValueError(f"Unexpected Shaping result status: {result.status}")

        if not inferred_runtime_messages:
            raise ValueError("Shaping Failed")

        filtered_messages = self.filter_runtime_messages(
            messages=inferred_runtime_messages,
            roles=["assistant"],
            visibility="user",
            kinds=["final"],
        )

        runtime_message = filtered_messages.pop()
        content_parts = self.filter_content_parts(
            parts=runtime_message.parts,
            part_types=["text"],
            encodings=["plain"],
            first_only=True,
        )

        if content_parts is None:
            raise ValueError("message returned empty")

        part = None
        if isinstance(content_parts, list):
            if 0 < len(content_parts) > 1:
                raise ValueError("contentpart is larger than 1 or zero")
            part = content_parts.pop()

        if isinstance(content_parts, ContentPart):
            part = content_parts

        if not part:
            raise ValueError("Content Part is None")

        if isinstance(part.data, str):
            try:
                result = json.loads(part.data)
            except json.JSONDecodeError as e:
                raise e
        else:
            result = part.data

        logging.info(f"shaping: {result}")

        if not isinstance(result, dict):
            raise ValueError(
                f"Task shaping returned unexpected type: {type(result).__name__}"
            )

        reply_items = result.get("reply_items")
        task_items = result.get("task_items")
        clarification_items = result.get("clarification_items")

        if not isinstance(reply_items, list):
            raise ValueError("Task shaping output missing valid 'reply_items'")
        if not isinstance(task_items, list):
            raise ValueError("Task shaping output missing valid 'task_items'")
        if not isinstance(clarification_items, list):
            raise ValueError("Task shaping output missing valid 'clarification_items'")

        return result

    async def _build_work_packet(
        self, *, user_id: str, shaped: dict[str, Any]
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
        estimated_tokens = self._get_total_token_estimation(
            messages=messages,
            reserved_output_tokens=reserved_tokens,
        )
        constraints = self._build_task_constraints(
            temperature=TEMPERATURE_POLICY.get(operation, 0.1), stream=False
        )

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

    async def _build_work_packets(
        self, *, user_id: str, shaped_tasks: list[dict[str, Any]]
    ) -> list[WorkPacket]:
        packets: list[WorkPacket] = []
        for task in shaped_tasks:
            packets.append(
                await self._build_work_packet(
                    user_id=user_id,
                    shaped=task,
                )
            )
        return packets

    async def _submit_packets(self, packets: list[WorkPacket]) -> list[WorkResult]:
        results: list[WorkResult] = []
        for packet in packets:
            results.append(await self.mailbox.submit(packet))
        return results

    async def _retrieve_results(
        self,
        list_of_deferred: list[tuple[WorkResult, WorkPacket]],
        *,
        timeout_seconds: float = 180.0,
        retry_sleep_seconds: float = 3.0,
    ) -> list[WorkResult]:
        results: list[WorkResult] = []

        now = time.monotonic()

        for accepted_result, _ in list_of_deferred:
            accepted_result.metadata.setdefault("deferred_started_at", now)

        pending = list(list_of_deferred)

        while pending:
            retry_later: list[tuple[WorkResult, WorkPacket]] = []
            now = time.monotonic()

            for accepted_result, packet in pending:
                backend = accepted_result.metadata.get("backend_desc")

                if backend is None:
                    results.append(
                        WorkResult(
                            status="failed",
                            work_id=packet.work_id,
                            backend_name=accepted_result.backend_name,
                            backend_model=accepted_result.backend_model,
                            error="Deferred work is missing backend descriptor",
                            metadata={"phase": "retrieve_results"},
                        )
                    )
                    continue

                started_at = float(accepted_result.metadata["deferred_started_at"])
                elapsed = now - started_at

                if elapsed >= timeout_seconds:
                    results.append(
                        WorkResult(
                            status="failed",
                            work_id=packet.work_id,
                            backend_name=accepted_result.backend_name,
                            backend_model=accepted_result.backend_model,
                            error=f"Deferred work timed out after {timeout_seconds:.1f} seconds",
                            metadata={
                                "phase": "retrieve_results",
                                "timeout_seconds": timeout_seconds,
                                "elapsed_seconds": elapsed,
                            },
                        )
                    )
                    continue

                result = await self.mailbox.retrieve(
                    packet=packet,
                    backend=backend,
                )

                if result.status == "running":
                    retry_later.append((accepted_result, packet))
                    continue

                if result.status in {"completed", "failed"}:
                    results.append(result)
                    continue

                results.append(
                    WorkResult(
                        status="failed",
                        work_id=packet.work_id,
                        backend_name=result.backend_name,
                        backend_model=result.backend_model,
                        error=f"Unexpected retrieved work status: {result.status}",
                        metadata={
                            "phase": "retrieve_results",
                            "raw_metadata": result.metadata,
                        },
                    )
                )

            pending = retry_later

            if pending:
                await asyncio.sleep(retry_sleep_seconds)

        list_of_deferred.clear()

        return results

    def _build_final_response_packet(
        self,
        *,
        inference_object: InferenceObject,
        prompt_context: dict[str, Any],
        sync_results: list[dict[str, Any]],
        deferred_results: list[dict[str, Any]],
        clarification_questions: list[str],
    ) -> WorkPacket:

        messages = build_final_response_messages(
            messages=inference_object.content,
            prompt_context=prompt_context,
            sync_results=sync_results,
            deferred_results=deferred_results,
            clarification_questions=clarification_questions,
        )

        constraints = self._build_task_constraints(
            temperature=TEMPERATURE_POLICY.get("analyze", 0),
            stream=inference_object.stream,
        )

        effective_tokens = self._get_total_token_estimation(
            reserved_output_tokens=MAX_TOKENS_POLICY.get("analyze", 1024),
            messages=messages,
        )

        ctask = packing.create_canonical_task_messages(
            work_type=WorkType.LLM,
            operation="analyze",
            messages=messages,
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
            id="final",
            disposition=WorkDisposition.DIRECT,
            metadata={
                "origin_stage": "main_ingress",
                "phase": "final_response",
            },
            task=ctask,
        )

        return ptask

    def _extract_clarification_questions(
        self,
        *,
        interpreted: dict[str, Any] | list[dict[str, Any]],
        shaped: dict[str, Any],
    ) -> list[str]:
        clarification_ids = set(shaped.get("clarification_items", []))
        questions: list[str] = []

        if isinstance(interpreted, list):
            items = interpreted
        elif isinstance(interpreted, dict):
            items = interpreted.get("items", [])
        else:
            items = []

        for item in items:
            if (
                isinstance(item, dict)
                and item.get("id") in clarification_ids
                and item.get("clarification_question")
            ):
                questions.append(item["clarification_question"])

        return questions

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
