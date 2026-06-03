from __future__ import annotations

from typing import Any

from cortex.cluster.deployment.types import (
    PodDeploymentSpec,
    PodServiceSpec,
    StorageProfile,
)


def _deployment_labels(spec: PodDeploymentSpec) -> dict[str, str]:
    labels = {
        "app": spec.name,
    }

    labels.update(spec.labels)
    return labels


def _service_labels(spec: PodServiceSpec) -> dict[str, str]:
    labels = dict(spec.labels)

    if spec.discovery is not None:
        labels["orin.ai/backend"] = "true"
        labels["orin.ai/role"] = spec.discovery.role

    return labels


def _service_annotations(spec: PodServiceSpec) -> dict[str, str]:
    discovery = spec.discovery

    if discovery is None:
        return {}

    annotations: dict[str, str] = {
        "orin.ai/kind": discovery.kind,
        "orin.ai/role": discovery.role,
        "orin.ai/visibility": discovery.visibility,
    }

    annotations.update(discovery.extra_annotations)

    return annotations


def build_pvc_body(spec: PodDeploymentSpec) -> dict[str, Any] | None:
    if spec.pvc is None:
        return None

    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": spec.pvc.name,
            "namespace": spec.namespace,
        },
        "spec": {
            "accessModes": spec.pvc.access_modes,
            "storageClassName": spec.pvc.storage_class_name,
            "resources": {
                "requests": {
                    "storage": spec.pvc.size,
                }
            },
        },
    }


def pvc_size_for_profile(profile: StorageProfile | None) -> str:
    if profile is None:
        return "20Gi"

    if profile == "light":
        return "20Gi"

    if profile == "medium":
        return "50Gi"

    if profile == "high":
        return "100Gi"

    raise ValueError(f"Unsupported storage profile: {profile}")


def build_deployment_body(spec: PodDeploymentSpec) -> dict[str, Any]:
    labels = _deployment_labels(spec)
    container_name = spec.container_name or spec.name
    volume_name = f"{spec.name}-data"

    container: dict[str, Any] = {
        "name": container_name,
        "image": spec.image,
        "imagePullPolicy": spec.image_pull_policy,
        "ports": [
            {
                "containerPort": spec.port,
                "name": "http",
                "protocol": "TCP",
            }
        ],
        "readinessProbe": {
            "httpGet": {
                "path": spec.probes.ready_path,
                "port": spec.port,
            },
            "initialDelaySeconds": spec.probes.readiness_initial_delay_seconds,
            "periodSeconds": spec.probes.readiness_period_seconds,
            "timeoutSeconds": spec.probes.readiness_timeout_seconds,
            "failureThreshold": spec.probes.readiness_failure_threshold,
        },
        "livenessProbe": {
            "httpGet": {
                "path": spec.probes.live_path,
                "port": spec.port,
            },
            "initialDelaySeconds": spec.probes.liveness_initial_delay_seconds,
            "periodSeconds": spec.probes.liveness_period_seconds,
            "timeoutSeconds": spec.probes.liveness_timeout_seconds,
            "failureThreshold": spec.probes.liveness_failure_threshold,
        },
        "startupProbe": {
            "httpGet": {
                "path": spec.probes.startup_path,
                "port": spec.port,
            },
            "periodSeconds": spec.probes.startup_period_seconds,
            "timeoutSeconds": spec.probes.startup_timeout_seconds,
            "failureThreshold": spec.probes.startup_failure_threshold,
        },
    }

    if spec.env:
        container["env"] = [
            {
                "name": key,
                "value": value,
            }
            for key, value in spec.env.items()
        ]

    if spec.env_from_config_maps:
        container["envFrom"] = [
            {
                "configMapRef": {
                    "name": config_map_name,
                }
            }
            for config_map_name in spec.env_from_config_maps
        ]

    volumes: list[dict[str, Any]] = []

    if spec.pvc is not None:
        container["volumeMounts"] = [
            {
                "name": volume_name,
                "mountPath": spec.pvc.mount_path,
            }
        ]

        volumes.append(
            {
                "name": volume_name,
                "persistentVolumeClaim": {
                    "claimName": spec.pvc.name,
                },
            }
        )

    pod_spec: dict[str, Any] = {
        "enableServiceLinks": False,
        "containers": [container],
    }

    if spec.runtime_class_name is not None:
        pod_spec["runtimeClassName"] = spec.runtime_class_name

    if spec.service_account_name is not None:
        pod_spec["serviceAccountName"] = spec.service_account_name

    if spec.node_selector:
        pod_spec["nodeSelector"] = spec.node_selector

    if spec.affinity is not None:
        pod_spec["affinity"] = spec.affinity

    if volumes:
        pod_spec["volumes"] = volumes

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": spec.name,
            "namespace": spec.namespace,
            "labels": labels,
        },
        "spec": {
            "replicas": spec.replicas,
            "selector": {
                "matchLabels": {
                    "app": spec.name,
                }
            },
            "template": {
                "metadata": {
                    "labels": labels,
                },
                "spec": pod_spec,
            },
        },
    }


def build_service_body(spec: PodServiceSpec) -> dict[str, Any]:
    app_name = spec.app_name or spec.name

    if spec.node_port is not None and spec.service_type != "NodePort":
        raise ValueError("node_port can only be used when service_type='NodePort'")

    port_body: dict[str, Any] = {
        "name": "http",
        "port": spec.port,
        "targetPort": spec.target_port,
        "protocol": "TCP",
    }

    if spec.node_port is not None:
        port_body["nodePort"] = spec.node_port

    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": spec.name,
            "namespace": spec.namespace,
            "labels": _service_labels(spec),
            "annotations": _service_annotations(spec),
        },
        "spec": {
            "selector": {
                "app": app_name,
            },
            "ports": [port_body],
            "type": spec.service_type,
        },
    }
