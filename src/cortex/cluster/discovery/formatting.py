from __future__ import annotations

from collections.abc import Iterable

from cortex.cluster.discovery.types import BackendDescriptor


def format_backend_list(
    backends: Iterable[BackendDescriptor],
    *,
    role_filter: str | None = None,
    verbose: bool = False,
) -> str:
    title = "Discovered backends"
    if role_filter:
        title += f" for role: {role_filter}"

    lines: list[str] = [
        title,
        "=" * len(title),
        "",
    ]

    for index, backend in enumerate(backends, start=1):
        lines.extend(
            format_backend_descriptor(
                backend,
                index=index,
                verbose=verbose,
            )
        )
        lines.append("")

    return "\n".join(lines).rstrip()


def format_backend_descriptor(
    backend: BackendDescriptor,
    *,
    index: int,
    verbose: bool = False,
) -> list[str]:
    health = backend.health
    runtime = backend.runtime

    status = getattr(health.status, "status", health.status)
    ready_endpoints = health.ready_endpoints
    http_healthy = health.http_healthy
    model_healthy = health.model_healthy

    effective_n_ctx = runtime.effective_n_ctx

    model = backend.model or "-"
    kind = backend.kind or "-"
    models_path = backend.model_status_path or "-"

    lines = [
        f"[{index}] {backend.name}",
        f"  service: {backend.namespace}/{backend.service_name}",
        f"  role: {backend.role}",
        f"  kind: {kind}",
        f"  model: {model}",
        f"  health: {status or 'unknown'}",
    ]

    if ready_endpoints is not None:
        lines.append(f"  ready endpoints: {ready_endpoints}")

    if effective_n_ctx:
        lines.append(f"  effective context: {effective_n_ctx}")

    lines.append(
        "  capabilities: "
        + (", ".join(backend.capabilities) if backend.capabilities else "-")
    )

    lines.append(
        "  modalities: "
        + (", ".join(backend.modalities) if backend.modalities else "-")
    )

    if verbose:
        lines.extend(
            [
                f"  url: {backend.url}",
                f"  visibility: {backend.visibility}",
                f"  priority: {backend.priority}",
                f"  weight: {backend.weight}",
                f"  http healthy: {http_healthy if http_healthy is not None else '-'}",
                f"  model healthy: {model_healthy if model_healthy is not None else '-'}",
                f"  health path: {backend.health_path}",
                f"  work path: {backend.work_path}",
                f"  models path: {models_path}",
            ]
        )

    return lines