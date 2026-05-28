# src/federation/capabilities.py

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

import httpx

from .models import CapabilitySummary, FederationNetwork, NetworkMember
from .policy import RUNTIME_OPERATIONS
from .settings import FederationSettings, get_settings


class CapabilityError(RuntimeError):
    pass


class CapabilitySourceError(CapabilityError):
    pass


PUBLIC_WORK_TYPES: set[str] = {"llm", "tool"}

DEFAULT_OPERATIONS_BY_WORK_TYPE: dict[str, list[str]] = {
    "llm": [
        "chat",
        "summarize",
        "classify",
        "extract",
        "analyze",
    ],
    "tool": [
        "search",
        "inspect",
    ],
}

DEFAULT_MODALITIES_BY_WORK_TYPE: dict[str, list[str]] = {
    "llm": ["text"],
    "tool": ["text"],
}


class CapabilityService:
    """
    Builds sanitized Federation capability summaries.

    This service may inspect local Cortex discovery state, but it must never
    expose internal cluster structure externally.

    Do not expose:
      - pod names
      - node IPs
      - internal service URLs
      - Kubernetes labels
      - Kubernetes annotations
      - SSH details
      - model file paths
      - deployment metadata
      - memory contents
      - slash commands
      - backend load/unload controls
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: FederationSettings | None = None,
    ) -> None:
        self.http = http
        self.settings = settings or get_settings()

    async def summarize_local_capabilities(
        self,
        *,
        network: FederationNetwork | None = None,
    ) -> list[CapabilitySummary]:
        """
        Read local backend discovery data from Cortex and return sanitized
        Federation capability summaries.

        If a network is supplied, its policy is used to further restrict the
        advertised operations and work types.
        """

        backends = await self._fetch_cortex_backends()

        summaries = [
            summary
            for backend in backends
            if (summary := summarize_backend_descriptor(backend, network=network))
            is not None
        ]

        return merge_capability_summaries(summaries, network=network)

    async def publish_local_capabilities_to_member(
        self,
        *,
        member_service: Any,
        network: FederationNetwork,
        member: NetworkMember,
    ) -> NetworkMember:
        """
        Convenience helper for updating this node's member record with sanitized
        local capabilities.

        member_service is intentionally duck-typed to avoid a hard import cycle
        with members.py.
        """

        capabilities = await self.summarize_local_capabilities(network=network)

        return await member_service.update_capabilities(
            network_id=network.network_id,
            cluster_id=member.cluster_id,
            capabilities=capabilities,
        )

    async def _fetch_cortex_backends(self) -> list[dict[str, Any]]:
        url = self.settings.cortex_network_url()

        try:
            response = await self.http.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CapabilitySourceError(
                f"Failed to read Cortex network state: {exc}"
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise CapabilitySourceError(
                "Cortex /network returned invalid JSON"
            ) from exc

        return extract_backend_list(payload)


def extract_backend_list(payload: Any) -> list[dict[str, Any]]:
    """
    Accepts several likely Cortex /network response shapes:

      [backend, backend]
      {"backends": [...]}
      {"network": [...]}
      {"items": [...]}
      {"ok": true, "backends": [...]}
    """

    if isinstance(payload, list):
        return [_dict_like(item) for item in payload if _dict_like(item)]

    if not isinstance(payload, dict):
        raise CapabilitySourceError("Cortex /network response must be object or list")

    for key in ("result", "backends", "network", "items", "descriptors"):
        value = payload.get(key)

        if isinstance(value, list):
            return [_dict_like(item) for item in value if _dict_like(item)]

    # Some endpoints return role-grouped candidates:
    # {"llm": [...], "tool": [...]}.
    grouped: list[dict[str, Any]] = []

    for value in payload.values():
        if isinstance(value, list):
            grouped.extend(_dict_like(item) for item in value if _dict_like(item))

    if grouped:
        return grouped

    return []


def summarize_backend_descriptor(
    backend: Any,
    *,
    network: FederationNetwork | None = None,
) -> CapabilitySummary | None:
    """
    Convert a local BackendDescriptor-like object into a public-safe
    CapabilitySummary.
    """

    desc = _dict_like(backend)

    if not desc:
        return None

    role = _string_value(desc, "role") or _string_value(desc, "work_type")

    if role is None:
        return None

    work_type = normalize_work_type(role)

    if work_type not in PUBLIC_WORK_TYPES:
        return None

    if network is not None and work_type not in network.policy.allowed_work_types:
        return None

    operations = extract_operations(desc, work_type=work_type)

    if network is not None:
        operations = [
            op
            for op in operations
            if op in network.policy.allowed_operations and op in RUNTIME_OPERATIONS
        ]
    else:
        operations = [op for op in operations if op in RUNTIME_OPERATIONS]

    if not operations:
        return None

    modalities = extract_modalities(desc, work_type=work_type)

    max_context_hint = extract_context_hint(desc, network=network)
    max_result_tokens_hint = extract_result_tokens_hint(desc, network=network)

    metadata: dict[str, Any] = {}

    if network is not None and network.policy.expose_runtime_metadata:
        runtime_metadata = extract_safe_runtime_metadata(desc)

        if runtime_metadata:
            metadata["runtime"] = runtime_metadata

    if network is not None and network.policy.expose_exact_models:
        model = _string_value(desc, "model")

        if model:
            metadata["model"] = model

    return CapabilitySummary(
        work_type=work_type,
        operations=operations,
        modalities=modalities,
        max_context_hint=max_context_hint,
        max_result_tokens_hint=max_result_tokens_hint,
        availability_hint=extract_availability_hint(desc),
        latency_hint=extract_latency_hint(desc),
        metadata=metadata,
    )


def merge_capability_summaries(
    summaries: Iterable[CapabilitySummary],
    *,
    network: FederationNetwork | None = None,
) -> list[CapabilitySummary]:
    """
    Merge multiple backend summaries into one public summary per work_type.

    This avoids exposing how many internal pods/services exist.
    """

    grouped: dict[str, list[CapabilitySummary]] = defaultdict(list)

    for summary in summaries:
        grouped[summary.work_type].append(summary)

    merged: list[CapabilitySummary] = []

    for work_type, items in grouped.items():
        operations = sorted({op for item in items for op in item.operations})
        modalities = sorted({mod for item in items for mod in item.modalities})

        context_values = [
            item.max_context_hint
            for item in items
            if isinstance(item.max_context_hint, int)
        ]

        result_values = [
            item.max_result_tokens_hint
            for item in items
            if isinstance(item.max_result_tokens_hint, int)
        ]

        if network is not None:
            operations = [
                op
                for op in operations
                if op in network.policy.allowed_operations and op in RUNTIME_OPERATIONS
            ]

            if work_type not in network.policy.allowed_work_types:
                continue

        if not operations:
            continue

        merged.append(
            CapabilitySummary(
                work_type=work_type,
                operations=operations,
                modalities=modalities
                or DEFAULT_MODALITIES_BY_WORK_TYPE.get(
                    work_type,
                    ["text"],
                ),
                max_context_hint=min(context_values) if context_values else None,
                max_result_tokens_hint=min(result_values) if result_values else None,
                availability_hint=merge_availability_hint(items),
                latency_hint=merge_latency_hint(items),
                metadata={},
            )
        )

    merged.sort(key=lambda item: item.work_type)
    return merged


def normalize_work_type(role: str) -> str:
    role = role.strip().lower()

    if role in {"llm", "model", "inference"}:
        return "llm"

    if role in {"tool", "tools", "search"}:
        return "tool"

    return role


def extract_operations(
    desc: dict[str, Any],
    *,
    work_type: str,
) -> list[str]:
    raw_capabilities = _list_of_strings(desc.get("capabilities"))

    operations: list[str] = []

    for value in raw_capabilities:
        normalized = value.strip().lower()

        # Accept either "chat" or "llm.chat".
        if "." in normalized:
            prefix, operation = normalized.split(".", 1)

            if normalize_work_type(prefix) != work_type:
                continue

            normalized = operation

        if normalized in RUNTIME_OPERATIONS:
            operations.append(normalized)

    if operations:
        return sorted(set(operations))

    return DEFAULT_OPERATIONS_BY_WORK_TYPE.get(work_type, [])


def extract_modalities(
    desc: dict[str, Any],
    *,
    work_type: str,
) -> list[str]:
    modalities = _list_of_strings(desc.get("modalities"))

    if modalities:
        return sorted(set(modalities))

    return DEFAULT_MODALITIES_BY_WORK_TYPE.get(work_type, ["text"])


def extract_context_hint(
    desc: dict[str, Any],
    *,
    network: FederationNetwork | None = None,
) -> int | None:
    value = _first_int(
        desc,
        keys=(
            "context_window",
            "max_context_tokens",
            "effective_n_ctx",
        ),
    )

    runtime = desc.get("runtime")

    if value is None and isinstance(runtime, dict):
        value = _first_int(
            runtime,
            keys=(
                "effective_n_ctx",
                "context_window",
                "max_context_tokens",
            ),
        )

    if value is None:
        return None

    if network is not None:
        return min(value, network.policy.max_context_tokens)

    return value


def extract_result_tokens_hint(
    desc: dict[str, Any],
    *,
    network: FederationNetwork | None = None,
) -> int | None:
    value = _first_int(
        desc,
        keys=(
            "max_output_tokens",
            "max_result_tokens",
            "max_new_tokens",
            "num_predict",
        ),
    )

    if value is None:
        return None

    if network is not None:
        return min(value, network.policy.max_result_tokens)

    return value


def extract_availability_hint(desc: dict[str, Any]) -> str | None:
    health = desc.get("health")

    if isinstance(health, dict):
        status = health.get("status")

        if isinstance(status, str):
            if status == "healthy":
                return "available"
            if status == "degraded":
                return "degraded"
            if status == "unavailable":
                return "unavailable"

    status = desc.get("status")

    if isinstance(status, str):
        return status

    return None


def extract_latency_hint(desc: dict[str, Any]) -> str | None:
    role = _string_value(desc, "role")

    if role in {"fast", "light"}:
        return "low"

    priority = _first_int(desc, keys=("priority",))

    if priority is not None and priority >= 100:
        return "preferred"

    return None


def merge_availability_hint(items: list[CapabilitySummary]) -> str | None:
    hints = {item.availability_hint for item in items if item.availability_hint}

    if "available" in hints:
        return "available"

    if "degraded" in hints:
        return "degraded"

    if "unavailable" in hints:
        return "unavailable"

    return None


def merge_latency_hint(items: list[CapabilitySummary]) -> str | None:
    hints = {item.latency_hint for item in items if item.latency_hint}

    if "low" in hints:
        return "low"

    if "preferred" in hints:
        return "preferred"

    if hints:
        return sorted(hints)[0]

    return None


def extract_safe_runtime_metadata(desc: dict[str, Any]) -> dict[str, Any]:
    """
    Only expose coarse runtime hints.

    Never expose service URLs, node data, labels, annotations, deployment
    information, model paths, or backend refs.
    """

    safe: dict[str, Any] = {}

    context_hint = extract_context_hint(desc, network=None)

    if context_hint is not None:
        safe["context_window_hint"] = context_hint

    result_hint = extract_result_tokens_hint(desc, network=None)

    if result_hint is not None:
        safe["max_result_tokens_hint"] = result_hint

    provider = _string_value(desc, "provider")

    if provider:
        safe["provider_hint"] = provider

    protocol = _string_value(desc, "protocol")

    if protocol:
        safe["protocol_hint"] = protocol

    return safe


def public_member_capability_view(
    member: NetworkMember,
    *,
    network: FederationNetwork,
) -> dict[str, Any]:
    """
    Public-safe member capability view.

    This does not expose member public keys unless a later policy explicitly
    allows it.
    """

    capabilities = merge_capability_summaries(
        member.advertised_capabilities,
        network=network,
    )

    return {
        "network_id": member.network_id,
        "cluster_id": member.cluster_id,
        "role": member.role,
        "status": member.status,
        "last_seen_at": (
            member.last_seen_at.isoformat() if member.last_seen_at else None
        ),
        "capabilities": [
            capability.model_dump(mode="json") for capability in capabilities
        ],
    }


def _dict_like(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value

    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {}

    if hasattr(value, "__dict__"):
        return dict(value.__dict__)

    return {}


def _string_value(values: dict[str, Any], key: str) -> str | None:
    value = values.get(key)

    if isinstance(value, str) and value.strip():
        return value.strip()

    return None


def _list_of_strings(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, str):
        return [value]

    if not isinstance(value, list):
        return []

    result: list[str] = []

    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())

    return result


def _first_int(
    values: dict[str, Any],
    *,
    keys: tuple[str, ...],
) -> int | None:
    for key in keys:
        value = values.get(key)

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
