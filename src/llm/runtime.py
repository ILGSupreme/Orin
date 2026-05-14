from fastapi.responses import StreamingResponse

from common.primer import Primer
from common.protocol.routing_types import (
    WorkPacket,
    WorkResult,
)


class GenerativeModelRuntime:
    def __init__(self, *, primer: Primer) -> None:
        self.primer = primer
        self.work_responses = {}

    async def handle_ingress(self, req: WorkPacket) -> WorkResult:
        task = req.task

        rsp = self.primer.send_work_to_thread(
            messages=task.messages,
            constraints=task.constraints,
            operation=task.operation,
        )

        self.work_responses[req.work_id] = {
            "task": rsp,
            "engine": self.primer.engine_type,
            "stream": task.constraints.get("stream", False),
        }

        return WorkResult(status="accepted", work_id=req.work_id)

    async def handle_egress(self, work_id):

        work_item = self.work_responses.get(work_id, None)

        if work_item is None:
            return WorkResult(
                status="Not found",
                work_id=work_id,
                backend_model=self.primer.get_model(),
            )

        if self.primer.engine_type != work_item["engine"]:
            raise Exception("primer backend is not of the correct instance")
        if self.primer.is_ready() is False:
            raise Exception("primer is not ready")
        if work_item["stream"]:
            async_gen = self.primer.stream_text(**work_item["task"])

            return StreamingResponse(
                async_gen,
                media_type="text/plain",
            )
        else:
            if not work_item["task"].done():
                return WorkResult(
                    status="running",
                    work_id=work_id,
                    backend_model=self.primer.get_model(),
                )
            try:
                messages = work_item["task"].result()

                # temporary
                if self.primer._message_adapter:
                    inferred_runtime_messages = (
                        self.primer._message_adapter.parse_response(
                            content=messages, role="assistant"
                        )
                    )
                else:
                    raise ValueError("Message Adapter not set")

            except Exception as exc:
                self.work_responses.pop(work_id)
                return WorkResult(
                    status="failed",
                    work_id=work_id,
                    content=[],
                    error=str(exc),
                    backend_model=self.primer.get_model(),
                )
            self.work_responses.pop(work_id)
            return WorkResult(
                status="completed",
                work_id=work_id,
                content=inferred_runtime_messages,
                backend_model=self.primer.get_model(),
            )
