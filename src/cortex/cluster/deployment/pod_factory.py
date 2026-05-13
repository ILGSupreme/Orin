from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from cortex.cluster.deployment.types import (
    MachinePlatform,
    PodDeploymentSpec,
    PodDiscoverySpec,
    PodPvcSpec,
    PodServiceSpec,
)

PodAppKind = Literal["cortex", "llm", "tool", "memory"]


@dataclass(slots=True)
class PodImageConfig:
    registry: str = "orin-gw:5000"
    version: str = "1.0.0"

    def image_for(self, kind: PodAppKind, platform: MachinePlatform) -> str:
        if platform == "jetson":
            return f"{self.registry}/{kind}-jetson:{self.version}"

        if platform in {"arm64", "amd64"}:
            return f"{self.registry}/{kind}:{self.version}"

        raise ValueError(f"Unsupported platform: {platform}")


@dataclass(slots=True)
class PodFactoryDefaults:
    namespace: str
    images: PodImageConfig = field(default_factory=PodImageConfig)

    default_port: int = 8080
    cortex_node_port: int = 30080

    pvc_size: str = "30Gi"
    pvc_storage_class_name: str = "local-path"
    pvc_mount_path: str = "/models/huggingface"

    image_pull_policy: Literal["Always", "IfNotPresent", "Never"] = "Always"
    runtime_class_name: str | None = "nvidia"

    cortex_service_account_name: str | None = "cortex"


def normalize_instance(instance: str | None) -> str | None:
    if instance is None:
        return None

    value = instance.strip().lower().replace("_", "-")

    if not value:
        return None

    return value


def pod_name(kind: PodAppKind | str, instance: str | None = None) -> str:
    instance = normalize_instance(instance)

    if instance is None:
        return kind

    return f"{kind}-{instance}"


def service_name_for(kind: PodAppKind | str, instance: str | None = None) -> str:
    return f"{pod_name(kind, instance)}-service"


def pvc_name_for(kind: PodAppKind | str, instance: str | None = None) -> str:
    return f"{pod_name(kind, instance)}-pvc"


def config_map_name_for(kind: PodAppKind, instance: str | None = None) -> str:
    return f"{pod_name(kind, instance)}-config"


def node_selector_for(
    kind: PodAppKind,
    instance: str | None = None,
) -> dict[str, str]:
    selector = {
        f"orin.role.{kind}": "true",
    }

    instance = normalize_instance(instance)

    if instance is not None:
        selector[f"orin.{kind}.{instance}"] = "true"

    return selector


def discovery_for(kind: PodAppKind) -> PodDiscoverySpec:
    models_path = "/models" if kind == "llm" else None

    return PodDiscoverySpec(
        kind=kind,
        role=kind,
        visibility="internal",
        health_path="/health",
        work_path="/work",
        models_path=models_path,
    )


def default_capabilities_for(kind: PodAppKind) -> list[str]:
    if kind == "cortex":
        return ["cortex"]

    if kind == "llm":
        return ["chat"]

    if kind == "tool":
        return ["tool"]

    if kind == "memory":
        return ["memory"]

    raise ValueError(f"Unsupported pod kind: {kind}")


def default_modalities_for(kind: PodAppKind) -> list[str]:
    if kind == "llm":
        return ["text"]

    return []


def create_pod_app_specs(
    *,
    role: PodAppKind,
    app_name: str,
    platform: MachinePlatform,
    defaults: PodFactoryDefaults,
    image: str | None = None,
    config_map: str | None = None,
    env: dict[str, str] | None = None,
    pvc_enabled: bool = True,
    pvc_mount_path: str | None = None,
    pvc_size: str | None = None,
    runtime_class_name: str | None = None,
    node_selector: dict[str, str] | None = None,
    affinity: dict | None = None,
) -> tuple[PodDeploymentSpec, PodServiceSpec]:
    selected_image = image or defaults.images.image_for(role, platform)

    selected_runtime_class_name = (
        defaults.runtime_class_name
        if runtime_class_name is None
        else runtime_class_name
    )

    deployment = PodDeploymentSpec(
        name=app_name,
        image=selected_image,
        namespace=defaults.namespace,
        kind=role,
        container_name=role,
        port=defaults.default_port,
        image_pull_policy=defaults.image_pull_policy,
        runtime_class_name=selected_runtime_class_name,
        service_account_name=service_account_for(role, defaults),
        node_selector=node_selector or {},
        affinity=affinity,
        env=env or {},
        env_from_config_maps=[config_map] if config_map else [],
        pvc=(
            PodPvcSpec(
                name=pvc_name_for(app_name),
                mount_path=pvc_mount_path or defaults.pvc_mount_path,
                size=pvc_size or defaults.pvc_size,
                storage_class_name=defaults.pvc_storage_class_name,
            )
            if pvc_enabled
            else None
        ),
    )

    service = PodServiceSpec(
        name=service_name_for(app_name),
        namespace=defaults.namespace,
        app_name=app_name,
        port=defaults.default_port,
        target_port=defaults.default_port,
        service_type="NodePort" if role == "cortex" else "ClusterIP",
        node_port=defaults.cortex_node_port if role == "cortex" else None,
        discovery=discovery_for(role),
    )

    return deployment, service


def service_account_for(
    kind: PodAppKind,
    defaults: PodFactoryDefaults,
) -> str | None:
    if kind == "cortex":
        return defaults.cortex_service_account_name

    return None
