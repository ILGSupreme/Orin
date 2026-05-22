from typing import Any

from common.protocol.internal_types import InferenceObject
from common.protocol.memory_types import (
    ListMemoryClaim,
    MemoryClaim,
    MemoryEvent,
    Note,
    PromptContextRequest,
    ResolveSessionRequest,
    RetrievalRequest,
    Session,
    Summary,
    User,
)
from common.protocol.routing_types import (
    CanonicalTask,
    RoutingHints,
    WorkDisposition,
    WorkPacket,
    WorkType,
)
from common.protocol.unified_types import RuntimeMessage
from common.protocol.memory_types import BaseRuntimeMemoryRequest

from common.types import MEMORY_OPERATIONS


def create_workpacket(
    id: str,
    disposition: str,
    metadata: dict[str, Any],
    task: CanonicalTask,
):
    return WorkPacket(
        work_id=id,
        disposition=WorkDisposition(disposition),
        metadata=metadata,
        task=task,
    )


def create_canonical_task_messages(
    work_type,
    operation: str,
    messages: list[RuntimeMessage],
    inputs,
    constraints,
    routing_hints,
):
    return CanonicalTask(
        work_type=work_type,
        operation=operation,
        messages=messages,
        inputs=inputs,
        constraints=constraints,
        routing_hints=routing_hints,
    )


def create_canonical_task(
    wtype: WorkType,
    operation: str,
    content: list[RuntimeMessage] | BaseRuntimeMemoryRequest,
    input: dict[str, Any],
    constraints: dict[str, Any],
    routing_hints: RoutingHints,
) -> CanonicalTask:

    _ismemreq = None
    _ismessage = []
    if isinstance(content, list):
        _ismessage = content

    elif isinstance(content, BaseRuntimeMemoryRequest):
        _ismemreq = content

    else:
        raise ValueError("Content is of wrong type")

    return CanonicalTask(
        work_type=wtype,
        operation=operation,
        messages=_ismessage,
        memory_request=_ismemreq,
        inputs=input,
        constraints=constraints,
        routing_hints=routing_hints,
    )


def create_canonical_task_memory(
    work_type: WorkType,
    operation: str,
    memory_request: BaseRuntimeMemoryRequest,
    inputs: dict[str, Any],
    constraints: dict[str, Any],
    routing_hints: RoutingHints,
):
    return CanonicalTask(
        work_type=work_type,
        operation=operation,
        memory_request=memory_request,
        inputs=inputs,
        constraints=constraints,
        routing_hints=routing_hints,
    )


def create_memory_workpacket(
    memory_operation: MEMORY_OPERATIONS,
    inference_object: InferenceObject,
    memory_request: (
        Summary
        | Note
        | MemoryEvent
        | MemoryClaim
        | ListMemoryClaim
        | User
        | Session
        | dict[str, Any]
        | RetrievalRequest
        | PromptContextRequest
        | ResolveSessionRequest
    ),
) -> WorkPacket:

    runtime_message_request = BaseRuntimeMemoryRequest(
        user_id=inference_object.user_id,
        session_id=inference_object.session_id,
        channel=inference_object.channel,
        request=memory_request,
    )

    canonical_task = create_canonical_task(
        wtype=WorkType.MEMORY,
        operation=memory_operation,
        content=runtime_message_request,
        input={},
        constraints={},
        routing_hints=RoutingHints(role=WorkType.MEMORY),
    )

    packet = create_workpacket(
        id=f"{inference_object.user_id}:upsert_user",
        disposition=WorkDisposition.DIRECT,
        metadata={},
        task=canonical_task,
    )

    return packet
