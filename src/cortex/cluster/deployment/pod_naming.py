from __future__ import annotations

import re

from kubernetes import client

from cortex.cluster.deployment.helper import (
    deployment_exists,
    pvc_exists,
    service_exists,
)
from cortex.cluster.deployment.types import (
    MachinePlatform,
    PodAppKind,
)

_VALID_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


class PodNameError(ValueError):
    pass


def backend_base_selector() -> dict[str, str]:
    return {
        "orin.role.backend": "true",
    }


def platform_selector(platform: MachinePlatform) -> dict[str, str]:
    return {
        "orin.platform": platform,
    }


def preferred_role_affinity(role: PodAppKind) -> dict:
    return {
        "nodeAffinity": {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {
                    "weight": 100,
                    "preference": {
                        "matchExpressions": [
                            {
                                "key": f"orin.role.{role}",
                                "operator": "In",
                                "values": ["true"],
                            }
                        ]
                    },
                }
            ]
        }
    }


def validate_k8s_name(name: str) -> None:
    if not name:
        raise PodNameError("Name cannot be empty")

    if len(name) > 63:
        raise PodNameError("Name cannot be longer than 63 characters")

    if _VALID_NAME.match(name) is None:
        raise PodNameError(
            "Name must use lowercase letters, numbers, and hyphens only, "
            "and must start and end with a letter or number"
        )


def normalize_name_part(value: str) -> str:
    value = value.strip().lower().replace("_", "-")
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-+", "-", value)
    value = value.strip("-")

    validate_k8s_name(value)
    return value


def base_pod_name(role: str, size: str | None = None) -> str:
    role = normalize_name_part(role)

    if size is None:
        return role

    size = normalize_name_part(size)
    return f"{role}-{size}"


def service_name_for(app_name: str) -> str:
    return f"{app_name}-service"


def pvc_name_for(app_name: str) -> str:
    return f"{app_name}-pvc"


def pod_app_name_available(
    core: client.CoreV1Api,
    apps: client.AppsV1Api,
    *,
    namespace: str,
    app_name: str,
) -> bool:
    return not (
        deployment_exists(apps, namespace=namespace, name=app_name)
        or service_exists(core, namespace=namespace, name=service_name_for(app_name))
        or pvc_exists(core, namespace=namespace, name=pvc_name_for(app_name))
    )


def assert_pod_app_name_available(
    core: client.CoreV1Api,
    apps: client.AppsV1Api,
    *,
    namespace: str,
    app_name: str,
) -> None:
    validate_k8s_name(app_name)

    if not pod_app_name_available(
        core,
        apps,
        namespace=namespace,
        app_name=app_name,
    ):
        raise PodNameError(f"Pod app name already exists: {app_name}")


def next_available_pod_app_name(
    core: client.CoreV1Api,
    apps: client.AppsV1Api,
    *,
    namespace: str,
    role: str,
    size: str | None = None,
) -> str:
    base = base_pod_name(role, size)

    # For cortex/memory without size, prefer bare names first.
    if size is None and pod_app_name_available(
        core,
        apps,
        namespace=namespace,
        app_name=base,
    ):
        return base

    index = 1

    while True:
        candidate = f"{base}-{index}"
        validate_k8s_name(candidate)

        if pod_app_name_available(
            core,
            apps,
            namespace=namespace,
            app_name=candidate,
        ):
            return candidate

        index += 1


def labels_match(actual: dict[str, str], required: dict[str, str]) -> bool:
    return all(actual.get(key) == value for key, value in required.items())
