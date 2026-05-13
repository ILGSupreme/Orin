from typing import Any

from common.protocol.routing_types import (
    CanonicalTask,
    RoutingHints,
    WorkDisposition,
    WorkPacket,
    WorkType,
)
from common.protocol.unified_types import RuntimeMemoryRequest, RuntimeMessage


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


def create_canonical_task_memory(
    work_type: WorkType,
    operation: str,
    memory_request: RuntimeMemoryRequest,
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
