"""Session ingress for the shared persistent agent runtime."""

from __future__ import annotations

from fastapi.responses import StreamingResponse

from common.jobs import JobSpec
from common.protocol.egress_types import EgressResponse
from common.protocol.internal_types import InferenceObject
from common.protocol.routing_types import WorkResult
from common.protocol.unified_types import ContentPart, RuntimeMessage
from cortex.cortex import memory


class Harness:
    def __init__(self, *, router, job_manager, agent_runtime):
        self.router, self.job_manager, self.agent_runtime = (
            router,
            job_manager,
            agent_runtime,
        )

    async def ingression(self, ingression_type, request, stream=False):
        # Both natural ingress surfaces use the same flow; explicit slash commands
        # are dispatched by CortexRuntime before reaching Harness.
        inference = await self._validate_session(request)
        turn = await self.agent_runtime.handle_turn(inference)
        message = RuntimeMessage(
            role="assistant",
            parts=[ContentPart(type="text", data=turn.text)],
            metadata={"visibility": "user", "kind": "final"},
        )
        message.parts[0].metadata["inference_privacy"] = turn.privacy
        await memory.insert_message(
            router=self.router, inference_object=inference, message=[message]
        )
        metadata = {"mode": "unified", "goal_id": turn.goal_id, "job_id": turn.job_id}
        if stream:
            return StreamingResponse(
                iter([turn.text]),
                media_type="text/plain",
                headers={
                    "Session-Id": inference.session_id or "",
                    "Cache-Control": "no-cache",
                    "X-Orin-Goal-Id": turn.goal_id or "",
                    "X-Orin-Job-Id": turn.job_id or "",
                },
            )
        return EgressResponse(
            content=[message],
            work_id=turn.job_id,
            session_id=inference.session_id,
            metadata=metadata,
        )

    async def _validate_session(self, request):
        inference = InferenceObject.model_validate(request.model_dump())
        privacy = inference.metadata.get("privacy", self.agent_runtime.default_privacy)
        for message in inference.content:
            for part in message.parts:
                part.metadata["inference_privacy"] = privacy
        if not await memory.upsert_user(router=self.router, inference_object=inference):
            raise RuntimeError("Memory could not resolve the user")
        if not inference.session_id:
            resolved = await memory.resolve_session(
                router=self.router, inference_object=inference
            )
            inference.session_id = resolved.get("session", {}).get("id")
            if not inference.session_id:
                raise RuntimeError("Memory could not resolve the session")
        results = await memory.insert_message(
            router=self.router, inference_object=inference, message=inference.content
        )
        if any(result.status != "completed" for result in results):
            raise RuntimeError("Memory could not persist the incoming turn")
        return inference

    async def handle_work_packet(self, packet):
        if packet.task.work_type != "llm":
            return WorkResult(
                status="failed", work_id=packet.work_id, error="unknown work type"
            )
        job = self.job_manager.start(
            spec=JobSpec(
                kind="harness.work",
                payload={
                    "packet": packet.model_dump(mode="json"),
                },
            ),
            runner=lambda job: self._execute_packet(job, packet),
        )
        return WorkResult(status="accepted", work_id=job.job_id)

    async def _execute_packet(self, job, packet):
        result = await self.router.send(packet)
        if result.status in {"accepted", "running"}:
            result = await self.router.retrieve_completed_work(
                result.work_id,
                packet.task.work_type,
                packet.task.operation,
                result.metadata["backend_ref"],
            )
        return result.model_copy(update={"work_id": job.job_id})

    async def retrieve_work_packet(self, work_id):
        job = self.job_manager.get(work_id)
        if job is None:
            return WorkResult(status="failed", work_id=work_id, error="job not found")
        if job.status in {"accepted", "running"}:
            return WorkResult(status=job.status, work_id=work_id)
        if not job.get_result():
            return WorkResult(
                status="failed",
                work_id=work_id,
                error=job.error or "job completed without result",
            )
        return WorkResult.model_validate(job.get_result()).sanitize_for_federation()
