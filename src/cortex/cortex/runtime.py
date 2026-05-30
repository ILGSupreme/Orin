from __future__ import annotations

from typing import Literal, overload

from fastapi.responses import StreamingResponse

from common.primer import Primer
from common.protocol import ingress_types, unified_types
from common.protocol.egress_types import EgressResponse
from common.protocol.ingress_types import InferenceSession
from common.protocol.routing_types import (
    WorkPacket,
    WorkResult,
)
from common.protocol.unified_types import (
    ContentPart,
    RuntimeMessage,
)
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
        return await self.harness.handle_work_packet(packet=req)

    async def handle_egress(self, work_id: str) -> WorkResult:
        return await self.harness.retrieve_work_packet(work_id)

    # ---------------------------------------------------------------------------
    # Private Function Calls
    # ---------------------------------------------------------------------------

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
    def _primer_not_ready_response(
        self, stream: Literal[True]
    ) -> StreamingResponse: ...

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
