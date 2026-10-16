from typing import Any

from common.factory.packing import create_memory_workpacket
from common.protocol.internal_types import InferenceObject
from common.protocol.memory_types import (
    PromptContextRequest,
    ResolveSessionRequest,
    User,
)
from common.protocol.routing_types import WorkResult
from common.protocol.unified_types import PromptContextResponse, RuntimeMessage
from cortex.router.service import RouterService


async def upsert_user(
    router: RouterService, inference_object: InferenceObject
) -> dict[str, Any]:

    packet = create_memory_workpacket(
        memory_operation="upsert_user",
        inference_object=inference_object,
        memory_request=User(external_id=inference_object.user_id),
    )

    response = await router.send(packet=packet)

    if response.status == "completed":
        return response.metadata
    else:
        return {}


async def resolve_session(
    router: RouterService, inference_object: InferenceObject
) -> dict[str, Any]:

    packet = create_memory_workpacket(
        memory_operation="resolve_session",
        inference_object=inference_object,
        memory_request=ResolveSessionRequest(max_idle_minutes=60),
    )

    response = await router.send(packet=packet)

    if response.status == "completed":
        return response.metadata
    else:
        return {}


async def insert_message(
    router: RouterService,
    inference_object: InferenceObject,
    message: list[RuntimeMessage],
) -> list[WorkResult]:
    responses = []
    for msg in message:
        packet = create_memory_workpacket(
            memory_operation="create_message",
            inference_object=inference_object,
            memory_request=msg.model_dump(mode="json"),
        )

        response = await router.send(packet=packet)

        responses.append(response)

    return responses


async def get_prompts_context(
    router: RouterService,
    inference_object: InferenceObject,
    limit: int = 1,
    message_roles: list[str] | None = None,
):

    packet = create_memory_workpacket(
        memory_operation="prompt_context",
        inference_object=inference_object,
        memory_request=PromptContextRequest(
            query="",
            include_recent_messages=True,
            recent_message_limit=limit,
            recent_message_roles=message_roles or ["assistant"],
        ),
    )

    response = await router.send(packet=packet)

    if response.status == "completed":
        return PromptContextResponse.model_validate(response.metadata)
    else:
        return None


async def conversation_context(router, request, default_privacy):
    """Return only scoped text whose privacy permits the current inference policy."""
    packet = create_memory_workpacket(
        memory_operation="prompt_context", inference_object=request,
        memory_request=PromptContextRequest(query="", include_recent_messages=True,
            recent_message_limit=4, recent_message_roles=["user", "assistant"],
            include_claims=False, include_summaries=False, include_chunks=False),
    )
    response = await router.send(packet=packet)
    if response.status != "completed":
        raise RuntimeError("Conversation context unavailable")
    context = PromptContextResponse.model_validate(response.metadata)
    external = request.metadata.get("privacy", default_privacy) == "external_allowed"
    allowed = [message for message in context.recent_messages
               if not external or message.get("metadata", {}).get("inference_privacy", "local_only") == "external_allowed"]
    return [{"role": message["role"], "text": message["content"][:400]} for message in allowed[-2:]]
