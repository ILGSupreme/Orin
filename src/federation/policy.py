# src/federation/policy.py

from __future__ import annotations

import copy
import json
from typing import Any

from .models import FederationNetwork, NetworkMember, NetworkPolicy
from .settings import FederationSettings, get_settings


RUNTIME_OPERATIONS: set[str] = {
    "chat",
    "summarize",
    "classify",
    "extract",
    "analyze",
    "search",
    "inspect",
}


class FederationPolicyError(RuntimeError):
    pass


class InvalidFederatedPacketError(FederationPolicyError):
    pass


class RemoteWorkDisabledError(FederationPolicyError):
    pass


class ResultPollingDisabledError(FederationPolicyError):
    pass


class CapabilityPublishDisabledError(FederationPolicyError):
    pass


class WorkTypeNotAllowedError(FederationPolicyError):
    pass


class OperationNotAllowedError(FederationPolicyError):
    pass


class PayloadTooLargeError(FederationPolicyError):
    pass


class ContextTooLargeError(FederationPolicyError):
    pass


class ResultTooLargeError(FederationPolicyError):
    pass


class MemberPolicyError(FederationPolicyError):
    pass


def check_remote_work_allowed(policy: NetworkPolicy) -> None:
    if not policy.allow_remote_work_submission:
        raise RemoteWorkDisabledError("Remote work submission is disabled")


def check_result_polling_allowed(policy: NetworkPolicy) -> None:
    if not policy.allow_remote_result_polling:
        raise ResultPollingDisabledError("Remote result polling is disabled")


def check_capability_publish_allowed(policy: NetworkPolicy) -> None:
    if not policy.allow_member_capability_publish:
        raise CapabilityPublishDisabledError("Capability publishing is disabled")


def extract_task(packet: dict[str, Any]) -> dict[str, Any]:
    task = packet.get("task")

    if not isinstance(task, dict):
        raise InvalidFederatedPacketError("Federated packet must contain task object")

    return task


def extract_work_type(packet: dict[str, Any]) -> str:
    task = extract_task(packet)
    work_type = task.get("work_type")

    if not isinstance(work_type, str) or not work_type.strip():
        raise InvalidFederatedPacketError("Federated packet task.work_type is invalid")

    return work_type


def extract_operation(packet: dict[str, Any]) -> str:
    task = extract_task(packet)
    operation = task.get("operation")

    if not isinstance(operation, str) or not operation.strip():
        raise InvalidFederatedPacketError("Federated packet task.operation is invalid")

    return operation


def check_work_type_allowed(
    policy: NetworkPolicy,
    *,
    work_type: str,
) -> None:
    if work_type not in policy.allowed_work_types:
        raise WorkTypeNotAllowedError(f"Work type not allowed: {work_type}")


def check_operation_allowed(
    policy: NetworkPolicy,
    *,
    operation: str,
) -> None:
    if operation not in policy.allowed_operations:
        raise OperationNotAllowedError(f"Operation not allowed: {operation}")


def check_runtime_operation_known(operation: str) -> None:
    if operation not in RUNTIME_OPERATIONS:
        raise OperationNotAllowedError(f"Unknown runtime operation: {operation}")


def check_payload_size(
    policy: NetworkPolicy,
    packet: dict[str, Any],
    *,
    settings: FederationSettings | None = None,
) -> None:
    settings = settings or get_settings()

    payload_bytes = json.dumps(
        packet,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")

    max_payload_bytes = min(policy.max_payload_bytes, settings.max_request_bytes)

    if len(payload_bytes) > max_payload_bytes:
        raise PayloadTooLargeError(
            f"Federated packet exceeds payload limit: "
            f"{len(payload_bytes)} > {max_payload_bytes}"
        )


def check_context_limit(
    policy: NetworkPolicy,
    packet: dict[str, Any],
) -> None:
    task = extract_task(packet)
    constraints = task.get("constraints", {})

    if constraints is None:
        return

    if not isinstance(constraints, dict):
        raise InvalidFederatedPacketError("Federated packet task.constraints is invalid")

    requested_context = _first_int(
        constraints,
        keys=(
            "max_context_tokens",
            "context_tokens",
            "num_ctx",
            "n_ctx",
        ),
    )

    if requested_context is None:
        return

    if requested_context > policy.max_context_tokens:
        raise ContextTooLargeError(
            f"Requested context exceeds network policy: "
            f"{requested_context} > {policy.max_context_tokens}"
        )


def check_result_limit(
    policy: NetworkPolicy,
    packet: dict[str, Any],
) -> None:
    task = extract_task(packet)
    constraints = task.get("constraints", {})

    if constraints is None:
        return

    if not isinstance(constraints, dict):
        raise InvalidFederatedPacketError("Federated packet task.constraints is invalid")

    requested_result_tokens = _first_int(
        constraints,
        keys=(
            "max_result_tokens",
            "max_output_tokens",
            "max_tokens",
            "max_new_tokens",
            "num_predict",
        ),
    )

    if requested_result_tokens is None:
        return

    if requested_result_tokens > policy.max_result_tokens:
        raise ResultTooLargeError(
            f"Requested result tokens exceed network policy: "
            f"{requested_result_tokens} > {policy.max_result_tokens}"
        )


def check_member_policy(
    member: NetworkMember,
    *,
    work_type: str,
    operation: str,
) -> None:
    if member.status != "active":
        raise MemberPolicyError(
            f"Member is not active: {member.network_id}:{member.cluster_id}"
        )

    if work_type not in member.allowed_work_types:
        raise MemberPolicyError(
            f"Member is not allowed to submit work_type: {work_type}"
        )

    if operation not in member.allowed_operations:
        raise MemberPolicyError(
            f"Member is not allowed to submit operation: {operation}"
        )


def check_network_policy(
    network: FederationNetwork,
    *,
    packet: dict[str, Any],
    settings: FederationSettings | None = None,
) -> None:
    policy = network.policy

    check_remote_work_allowed(policy)

    work_type = extract_work_type(packet)
    operation = extract_operation(packet)

    check_runtime_operation_known(operation)
    check_work_type_allowed(policy, work_type=work_type)
    check_operation_allowed(policy, operation=operation)
    check_payload_size(policy, packet, settings=settings)
    check_context_limit(policy, packet)
    check_result_limit(policy, packet)


def check_federated_work_allowed(
    *,
    network: FederationNetwork,
    member: NetworkMember,
    packet: dict[str, Any],
    settings: FederationSettings | None = None,
) -> None:
    """
    Full policy check for received federated work.

    This checks:
      - network allows remote work
      - work_type is explicitly allowed
      - operation is explicitly allowed
      - operation is a known runtime operation
      - payload size
      - context/result limits when requested
      - member status and member-specific permissions

    It does not check:
      - envelope signature
      - membership existence
      - nonce replay
      - quotas/concurrency
      - WorkPacket Pydantic validation
    """

    check_network_policy(
        network,
        packet=packet,
        settings=settings,
    )

    work_type = extract_work_type(packet)
    operation = extract_operation(packet)

    check_member_policy(
        member,
        work_type=work_type,
        operation=operation,
    )


def sanitize_packet_for_cortex(
    *,
    packet: dict[str, Any],
    network: FederationNetwork,
    member: NetworkMember,
    request_id: str,
) -> dict[str, Any]:
    """
    Return a sanitized copy of the federated packet before it is submitted
    to Cortex.

    External peers should not control trusted internal metadata. This function
    strips dangerous top-level metadata and adds explicit federation origin
    metadata that Cortex can treat as untrusted external-origin context.
    """

    sanitized = copy.deepcopy(packet)

    metadata = sanitized.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    for key in (
        "backend_ref",
        "backend_desc",
        "selected_backend",
        "internal",
        "trusted",
        "admin",
        "command",
        "deployment",
        "kubernetes",
        "ssh",
    ):
        metadata.pop(key, None)

    metadata["federation"] = {
        "network_id": network.network_id,
        "network_slug": network.slug,
        "origin_cluster_id": member.cluster_id,
        "member_role": member.role,
        "request_id": request_id,
    }

    sanitized["metadata"] = metadata

    return sanitized


def _first_int(
    values: dict[str, Any],
    *,
    keys: tuple[str, ...],
) -> int | None:
    for key in keys:
        value = values.get(key)

        if value is None:
            continue

        if isinstance(value, bool):
            continue

        if isinstance(value, int):
            return value

        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                continue

    return None