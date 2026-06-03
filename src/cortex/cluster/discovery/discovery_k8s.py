from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from kubernetes import client, config

import common.system.configuration as configuration
from cortex.cluster.discovery.types import (
    BackendDescriptor,
    BackendHealth,
    BackendHealthStatus,
)


class KubernetesDiscoveryProvider:
    BACKEND_LABEL = "orin.ai/backend"
    ROLE_LABEL = "orin.ai/role"

    KIND_ANNOTATION = "orin.ai/kind"
    MODEL_ANNOTATION = "orin.ai/model"
    CAPABILITIES_ANNOTATION = "orin.ai/capabilities"
    MODALITIES_ANNOTATION = "orin.ai/modalities"
    PRIORITY_ANNOTATION = "orin.ai/priority"
    WEIGHT_ANNOTATION = "orin.ai/weight"
    VISIBILITY_ANNOTATION = "orin.ai/visibility"

    namespace : str

    def __init__(
        self
    ) -> None:
        cfg = configuration.get_configuration("cortex")
        self.namespace = cfg.namespace

        if cfg.in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config()

        self.core = client.CoreV1Api()
        self.discovery = client.DiscoveryV1Api()

    async def discover(self) -> list[BackendDescriptor]:
        services = await asyncio.to_thread(self._list_backend_services)
        endpoint_slices = await asyncio.to_thread(self._list_endpoint_slices)

        ready_map = self._build_ready_endpoint_map(endpoint_slices)

        backends: list[BackendDescriptor] = []
        for svc in services:
            descriptor = self._service_to_descriptor(svc, ready_map)
            if descriptor is not None:
                backends.append(descriptor)

        return backends

    def _list_backend_services(self):
        label_selector = f"{self.BACKEND_LABEL}=true"
        resp = self.core.list_namespaced_service(
            namespace=self.namespace,
            label_selector=label_selector,
        )
        return resp.items

    def _list_endpoint_slices(self):
        data = self.discovery.api_client.call_api(
            f"/apis/discovery.k8s.io/v1/namespaces/{self.namespace}/endpointslices",
            "GET",
            auth_settings=["BearerToken"],
            response_type="object",
            _return_http_data_only=True,
        )
        return data.get("items", [])

    def _build_ready_endpoint_map(self, endpoint_slices: list[dict]) -> dict[str, int]:
        ready_map: dict[str, int] = {}

        for eps in endpoint_slices:
            metadata = eps.get("metadata") or {}
            labels = metadata.get("labels") or {}
            service_name = labels.get("kubernetes.io/service-name")
            if not service_name:
                continue

            ready_count = 0
            for endpoint in eps.get("endpoints") or []:
                conditions = endpoint.get("conditions") or {}
                if conditions.get("ready") is True:
                    ready_count += 1

            ready_map[service_name] = ready_map.get(service_name, 0) + ready_count

        return ready_map

    def _service_to_descriptor(
        self,
        svc: Any,
        ready_map: dict[str, int],
    ) -> BackendDescriptor | None:
        metadata = svc.metadata
        spec = svc.spec

        labels = metadata.labels or {}
        annotations = metadata.annotations or {}

        role = labels.get(self.ROLE_LABEL)
        kind = annotations.get(self.KIND_ANNOTATION)
        model = annotations.get(self.MODEL_ANNOTATION)

        if not role:
            logging.warning(
                "Skipping backend service %s: missing required labels role",
                metadata.name,
            )
            return None

        port = self._pick_service_port(spec.ports or [])
        if port is None:
            logging.warning(
                "Skipping backend service %s: no usable port", metadata.name
            )
            return None

        url = f"http://{metadata.name}.{metadata.namespace}.svc.cluster.local:{port}"

        capabilities = self._parse_json_list(
            annotations.get(self.CAPABILITIES_ANNOTATION, "[]")
        )
        modalities = self._parse_json_list(
            annotations.get(self.MODALITIES_ANNOTATION, "[]")
        )

        descriptor = BackendDescriptor(
            name=f"{metadata.namespace}/{metadata.name}",
            namespace=metadata.namespace,
            service_name=metadata.name,
            url=url,
            role=role,
            kind=kind,
            model=model,
            capabilities=capabilities,
            modalities=modalities,
            priority=self._parse_int(
                annotations.get(self.PRIORITY_ANNOTATION),
                default=100,
            )
            or 100,
            weight=self._parse_float(
                annotations.get(self.WEIGHT_ANNOTATION),
                default=1.0,
            )
            or 1.0,
            visibility=annotations.get(self.VISIBILITY_ANNOTATION, "internal"),
            health_path="/health",
            work_path="/work",
            model_status_path=("/model_status" if role in ("llm", "cortex") else None),
            labels=dict(labels),
            annotations=dict(annotations),
        )

        ready_endpoints = ready_map.get(metadata.name, 0)
        descriptor.health = BackendHealth(
            registered=True,
            ready_endpoints=ready_endpoints,
            http_healthy=False,
            model_healthy=False,
            status=BackendHealthStatus(
                "degraded" if ready_endpoints > 0 else "unavailable", ""
            ),
        )

        return descriptor

    @staticmethod
    def _pick_service_port(ports: list[Any]) -> int | None:
        if not ports:
            return None

        for port in ports:
            if getattr(port, "name", None) == "http":
                return int(port.port)

        return int(ports[0].port)

    @staticmethod
    def _parse_json_list(raw: str) -> list[str]:
        try:
            value = json.loads(raw)
            if isinstance(value, list):
                return [str(x) for x in value]
        except Exception:
            pass
        return []

    @staticmethod
    def _parse_int(raw: str | None, default: int | None = None) -> int | None:
        if raw is None:
            return default
        try:
            return int(raw)
        except Exception:
            return default

    @staticmethod
    def _parse_float(raw: str | None, default: float | None = None) -> float | None:
        if raw is None:
            return default
        try:
            return float(raw)
        except Exception:
            return default
