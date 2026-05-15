from __future__ import annotations

import base64
import json
import shlex

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

import common.system.configuration as configuration
from cortex.cluster.deployment.pod_factory import PodFactoryDefaults, PodImageConfig
from cortex.cluster.deployment.pod_naming import (
    backend_base_selector,
    labels_match,
    platform_selector,
    preferred_role_affinity,
    pvc_exists,
    pvc_name_for,
    service_name_for,
)
from cortex.cluster.deployment.pod_template import (
    build_deployment_body,
    build_pvc_body,
    build_service_body,
)
from cortex.cluster.deployment.ssh import SSHError, run_ssh
from cortex.cluster.deployment.types import (
    Inventory,
    K3sInstallMode,
    K3sInstallResult,
    KubernetesConfig,
    Machine,
    MachinePlatform,
    Node,
    PlacementDecision,
    PodAppKind,
    PodDeploymentSpec,
    PodServiceSpec,
    SSHConfig,
)


class DeploymentError(RuntimeError):
    pass


class DeploymentService:
    def __init__(
        self,
    ) -> None:
        self.inventory = Inventory()

        ##load configuration file
        cfg = configuration.get_configuration("cortex")
        
        self.namespace = cfg.namespace

        self.pod_factory_defaults = self._build_pod_factory_defaults()

        if cfg.in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config()

        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()

        self.add_node(
            alias=cfg.alias,
            host=cfg.host,
            ip=cfg.ip,
            platform=cfg.platform,
            ssh_user=cfg.user,
            ssh_key=cfg.ssh_key,
            ssh_port=cfg.port,
            nvidia_gpu=cfg.nvidia_gpu,
            k3s_node_name=cfg.k3s_node_name,
        )

    @staticmethod
    def _pod_is_ready(pod) -> bool:
        statuses = pod.status.container_statuses or []

        if not statuses:
            return False

        return all(status.ready for status in statuses)

    @staticmethod
    def _format_api_error(exc: ApiException) -> str:
        parts = []

        if exc.status:
            parts.append(f"status={exc.status}")

        if exc.reason:
            parts.append(f"reason={exc.reason}")

        if exc.body:
            parts.append(f"body={exc.body}")

        return ", ".join(parts) if parts else str(exc)

    @staticmethod
    def _k8s_node_is_ready(k8s_node) -> bool:
        conditions = k8s_node.status.conditions or []

        for condition in conditions:
            if condition.type == "Ready":
                return condition.status == "True"

        return False

    def _check_passwordless_sudo(self, node: Node) -> None:
        try:
            run_ssh(
                node,
                "sudo -n true",
                check=True,
                prefer_ip=True,
                timeout_seconds=15,
            )
        except SSHError as exc:
            raise DeploymentError(
                "Target node SSH works, but the SSH user cannot run sudo non-interactively.\n"
                f"node: {node.alias}\n"
                f"user: {node.ssh_config.default_user}\n\n"
                "Fix on the target node with something like:\n"
                f"  sudo tee /etc/sudoers.d/orin-deploy >/dev/null <<'EOF'\n"
                f"  {node.ssh_config.default_user} ALL=(ALL) NOPASSWD:ALL\n"
                f"  EOF\n"
                "  sudo chmod 0440 /etc/sudoers.d/orin-deploy\n"
                "  sudo visudo -cf /etc/sudoers.d/orin-deploy"
            ) from exc

    def _build_pod_factory_defaults(self) -> PodFactoryDefaults:
        
        deployment_cfg = configuration.get_configuration("cortex").deployment

        return PodFactoryDefaults(
            namespace=self.namespace,
            images=PodImageConfig(),
            default_port=deployment_cfg.default_port,
            cortex_node_port=deployment_cfg.cortex_node_port,
            pvc_size=deployment_cfg.pvc_size,
            pvc_storage_class_name=
                deployment_cfg.pvc_storage_class_name
            ,
            pvc_mount_path=
                deployment_cfg.pvc_mount_path
            ,
            image_pull_policy=deployment_cfg.image_pull_policy,
            runtime_class_name=deployment_cfg.runtime_class_name,
            cortex_service_account_name=deployment_cfg.cortex_service_account_name
        )

    def add_node(
        self,
        *,
        alias: str,
        host: str,
        ip: str,
        platform: MachinePlatform,
        ssh_user: str = "ubuntu",
        ssh_key: str | None = "~/.ssh/id_ed25519",
        ssh_port: int = 22,
        nvidia_gpu: bool = False,
        k3s_node_name: str | None = None,
    ) -> Node:
        node = Node(
            alias=alias,
            machine_settings=Machine(
                host=host,
                ip=ip,
                platform=platform,
                nvidia_gpu=nvidia_gpu,
            ),
            ssh_config=SSHConfig(
                default_user=ssh_user,
                default_key=ssh_key,
                port=ssh_port,
                auth_method="ssh_key",
            ),
            kubernetes_settings=KubernetesConfig(
                k3s_node_name=k3s_node_name or alias,
                namespace=self.namespace,
            ),
        )

        self.inventory.addNode(node)
        return node

    def remove_node(self, alias: str) -> bool:
        return self.inventory.removeNode(alias)

    def get_node(self, alias: str) -> Node | None:
        return self.inventory.getNode(alias)

    def list_inventory_nodes(
        self,
        *,
        verbose: bool = False,
    ) -> list[dict]:
        result: list[dict] = []

        for node in self.inventory.nodes:
            k3s_node_name = (
                node.kubernetes_settings.k3s_node_name
                if node.kubernetes_settings is not None
                else "-"
            )

            k8s_status = "not installed"
            labels: dict[str, str] = {}

            if node.kubernetes_settings is not None:
                try:
                    k8s_node = self.core.read_node(name=k3s_node_name)
                    labels = k8s_node.metadata.labels or {}

                    ready = self._k8s_node_is_ready(k8s_node)
                    k8s_status = "ready" if ready else "not ready"

                except ApiException as exc:
                    if exc.status == 404:
                        k8s_status = "not found"
                    else:
                        raise DeploymentError(
                            f"Failed to read Kubernetes node {k3s_node_name}: "
                            f"{self._format_api_error(exc)}"
                        ) from exc

            item = {
                "alias": node.alias,
                "host": node.machine_settings.host,
                "ip": node.machine_settings.ip,
                "platform": node.machine_settings.platform,
                "nvidia_gpu": "true" if node.machine_settings.nvidia_gpu else "false",
                "k3s_node_name": k3s_node_name,
                "k8s_status": k8s_status,
            }

            if verbose:
                item["labels"] = labels

            result.append(item)

        result.sort(key=lambda item: item["alias"])
        return result

    def label_node(
        self,
        *,
        alias: str,
        priority_roles: list[PodAppKind] | None = None,
    ) -> dict[str, str]:

        def system_labels_for_node(node: Node) -> dict[str, str]:
            return {
                "orin.role.backend": "true",
                "orin.platform": node.machine_settings.platform,
                "orin.gpu": "true" if node.machine_settings.nvidia_gpu else "false",
            }

        def priority_role_labels(roles: list[PodAppKind] | None) -> dict[str, str]:
            labels: dict[str, str] = {}

            for role in roles or []:
                labels[f"orin.role.{role}"] = "true"

            return labels

        node = self.inventory.getNode(alias)

        if node is None:
            raise DeploymentError(f"Unknown node alias: {alias}")

        if node.kubernetes_settings is None:
            raise DeploymentError(f"Node has no Kubernetes settings: {alias}")

        k3s_node_name = node.kubernetes_settings.k3s_node_name

        try:
            self.core.read_node(name=k3s_node_name)
        except ApiException as exc:
            if exc.status == 404:
                raise DeploymentError(
                    f"Node is not installed or has not joined Kubernetes yet: {k3s_node_name}"
                ) from exc

            raise DeploymentError(
                f"Failed to read Kubernetes node {k3s_node_name}: "
                f"{self._format_api_error(exc)}"
            ) from exc

        labels = system_labels_for_node(node)
        labels.update(priority_role_labels(priority_roles))

        body = {
            "metadata": {
                "labels": labels,
            }
        }

        try:
            self.core.patch_node(
                name=k3s_node_name,
                body=body,
            )
        except ApiException as exc:
            raise DeploymentError(
                f"Failed to label Kubernetes node {k3s_node_name}: "
                f"{self._format_api_error(exc)}"
            ) from exc

        return labels

    def install_kubernetes(
        self,
        alias: str,
        *,
        mode: K3sInstallMode = "agent",
    ) -> K3sInstallResult:
        node = self.inventory.getNode(alias)

        if node is None:
            raise ValueError(f"Unknown node alias: {alias}")

        if node.kubernetes_settings is None:
            raise ValueError(f"Node has no Kubernetes settings: {alias}")

        self._check_passwordless_sudo(node)

        if mode == "server":
            install_exec = self._build_k3s_server_exec(node)

            command = " ".join(
                [
                    "curl -sfL https://get.k3s.io",
                    "|",
                    f"INSTALL_K3S_EXEC={shlex.quote(install_exec)}",
                    "sh -",
                ]
            )

            server_url = None

        elif mode == "agent":
            token = self._get_k3s_token()
            server_url = self._get_k3s_server_url()
            install_exec = self._build_k3s_agent_exec(node)

            command = " ".join(
                [
                    f"K3S_URL={shlex.quote(server_url)}",
                    f"K3S_TOKEN={shlex.quote(token)}",
                    f"INSTALL_K3S_EXEC={shlex.quote(install_exec)}",
                    "sh -c",
                    shlex.quote("curl -sfL https://get.k3s.io | sh -"),
                ]
            )

        else:
            raise ValueError(f"Unsupported K3s install mode: {mode}")

        result = run_ssh(
            node,
            command,
            check=True,
            prefer_ip=True,
            timeout_seconds=None,
        )

        return K3sInstallResult(
            alias=node.alias,
            node_name=node.kubernetes_settings.k3s_node_name,
            mode=mode,
            server_url=server_url,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def deploy_pod_app(
        self,
        *,
        deployment: PodDeploymentSpec,
        service: PodServiceSpec,
    ) -> None:
        self.ensure_namespace(deployment.namespace)

        pvc_body = build_pvc_body(deployment)

        if pvc_body is not None:
            assert deployment.pvc is not None

            self._create_pvc_if_missing(
                namespace=deployment.namespace,
                name=deployment.pvc.name,
                body=pvc_body,
            )

        self._upsert_deployment(
            namespace=deployment.namespace,
            name=deployment.name,
            body=build_deployment_body(deployment),
        )

        self._upsert_service(
            namespace=service.namespace,
            name=service.name,
            body=build_service_body(service),
        )

    def delete_pod_app(
        self,
        *,
        app_name: str,
        namespace: str | None = None,
        delete_pvc: bool = True,
    ) -> list[dict[str, str]]:
        namespace = namespace or self.namespace

        results: list[dict[str, str]] = []

        service_name = service_name_for(app_name)
        pvc_name = pvc_name_for(app_name)

        results.append(
            self._delete_service_if_exists(
                namespace=namespace,
                name=service_name,
            )
        )

        results.append(
            self._delete_deployment_if_exists(
                namespace=namespace,
                name=app_name,
            )
        )

        if delete_pvc:
            results.append(
                self._delete_pvc_if_exists(
                    namespace=namespace,
                    name=pvc_name,
                )
            )
        else:
            results.append(
                {
                    "kind": "pvc",
                    "name": pvc_name,
                    "status": "kept",
                }
            )

        return results

    def list_pod_apps(
        self,
        *,
        namespace: str | None = None,
    ) -> list[dict]:
        namespace = namespace or self.namespace

        deployments = self.apps.list_namespaced_deployment(
            namespace=namespace,
        )

        results: list[dict] = []

        for deployment in deployments.items:
            name = deployment.metadata.name

            if not name:
                continue

            deployment_labels = deployment.metadata.labels or {}
            template_labels = deployment.spec.template.metadata.labels or {}

            app_label = deployment_labels.get("app") or template_labels.get("app")

            if app_label is None:
                continue

            container = deployment.spec.template.spec.containers[0]
            image = container.image

            service_name = service_name_for(name)
            pvc_name = pvc_name_for(name)

            service = self._read_service_if_exists(
                namespace=namespace,
                name=service_name,
            )

            pvc_exist = pvc_exists(
                core=self.core,
                namespace=namespace,
                name=pvc_name,
            )

            service_annotations = {}
            service_labels = {}

            if service is not None:
                service_annotations = service.metadata.annotations or {}
                service_labels = service.metadata.labels or {}

            pods = self._list_pods_for_app(
                namespace=namespace,
                app_name=name,
            )

            ready_pods = sum(1 for pod in pods if pod["ready"])
            total_pods = len(pods)

            results.append(
                {
                    "name": name,
                    "namespace": namespace,
                    "kind": service_annotations.get("orin.ai/kind", "-"),
                    "role": service_labels.get(
                        "orin.ai/role",
                        service_annotations.get("orin.ai/role", "-"),
                    ),
                    "image": image,
                    "service": service_name if service is not None else "missing",
                    "pvc": pvc_name if pvc_exist else "missing",
                    "ready_pods": ready_pods,
                    "total_pods": total_pods,
                    "pods": pods,
                }
            )

        results.sort(key=lambda item: item["name"])
        return results

    def _delete_deployment_if_exists(
        self,
        *,
        namespace: str,
        name: str,
    ) -> dict[str, str]:
        try:
            self.apps.delete_namespaced_deployment(
                name=name,
                namespace=namespace,
                body=client.V1DeleteOptions(),
            )

            return {
                "kind": "deployment",
                "name": name,
                "status": "deleted",
            }

        except ApiException as exc:
            if exc.status == 404:
                return {
                    "kind": "deployment",
                    "name": name,
                    "status": "not found",
                }

            raise DeploymentError(
                f"Failed to delete deployment {namespace}/{name}: "
                f"{self._format_api_error(exc)}"
            ) from exc

    def _delete_service_if_exists(
        self,
        *,
        namespace: str,
        name: str,
    ) -> dict[str, str]:
        try:
            self.core.delete_namespaced_service(
                name=name,
                namespace=namespace,
                body=client.V1DeleteOptions(),
            )

            return {
                "kind": "service",
                "name": name,
                "status": "deleted",
            }

        except ApiException as exc:
            if exc.status == 404:
                return {
                    "kind": "service",
                    "name": name,
                    "status": "not found",
                }

            raise DeploymentError(
                f"Failed to delete service {namespace}/{name}: "
                f"{self._format_api_error(exc)}"
            ) from exc

    def _delete_pvc_if_exists(
        self,
        *,
        namespace: str,
        name: str,
    ) -> dict[str, str]:
        try:
            self.core.delete_namespaced_persistent_volume_claim(
                name=name,
                namespace=namespace,
                body=client.V1DeleteOptions(),
            )

            return {
                "kind": "pvc",
                "name": name,
                "status": "deleted",
            }

        except ApiException as exc:
            if exc.status == 404:
                return {
                    "kind": "pvc",
                    "name": name,
                    "status": "not found",
                }

            raise DeploymentError(
                f"Failed to delete PVC {namespace}/{name}: "
                f"{self._format_api_error(exc)}"
            ) from exc

    def _get_control_node(self) -> Node:
        if not self.inventory.nodes:
            raise DeploymentError("Inventory has no control node")

        return self.inventory.nodes[0]

    def _get_k3s_token(self) -> str:
        secret_name = "k3s-join-token"
        secret_key = "token"

        try:
            secret = self.core.read_namespaced_secret(
                name=secret_name,
                namespace=self.namespace,
            )
        except ApiException as exc:
            raise DeploymentError(
                f"Failed to read K3s token secret {self.namespace}/{secret_name}: "
                f"{self._format_api_error(exc)}"
            ) from exc

        data = secret.data or {}

        encoded_token = data.get(secret_key)

        if not encoded_token:
            raise DeploymentError(
                f"K3s token secret {self.namespace}/{secret_name} does not contain key: {secret_key}"
            )

        token = base64.b64decode(encoded_token).decode("utf-8").strip()

        if not token:
            raise DeploymentError("K3s token secret is empty")

        return token

    def _get_k3s_server_url(self) -> str:
        control_node = self._get_control_node()
        ip = control_node.machine_settings.ip

        if not ip:
            raise DeploymentError("Control node IP is missing")

        return f"https://{ip}:6443"

    def _build_k3s_server_exec(self, node: Node) -> str:
        settings = node.kubernetes_settings

        if settings is None:
            raise DeploymentError(f"Node has no Kubernetes settings: {node.alias}")

        parts: list[str] = [
            "server",
            f"--node-name {shlex.quote(settings.k3s_node_name)}",
            f"--data-dir {shlex.quote(settings.data_dir)}",
            f"--write-kubeconfig-mode {shlex.quote(settings.write_kubeconfig_mode)}",
            f"--default-local-storage-path {shlex.quote(settings.default_local_storage_path)}",
            f"--service-cidr {shlex.quote(settings.service_cidr)}",
            f"--cluster-cidr {shlex.quote(settings.cluster_cidr)}",
        ]

        for item in settings.disable:
            parts.append(f"--disable {shlex.quote(item)}")

        return " ".join(parts)

    def _build_k3s_agent_exec(self, node: Node) -> str:
        settings = node.kubernetes_settings

        if settings is None:
            raise DeploymentError(f"Node has no Kubernetes settings: {node.alias}")

        return " ".join(
            [
                "agent",
                f"--node-name {shlex.quote(settings.k3s_node_name)}",
                f"--data-dir {shlex.quote(settings.data_dir)}",
            ]
        )

    def ensure_namespace(self, namespace: str) -> None:
        try:
            self.core.read_namespace(name=namespace)
            return
        except ApiException as exc:
            if exc.status != 404:
                raise DeploymentError(
                    f"Failed to read namespace {namespace}: {self._format_api_error(exc)}"
                ) from exc

        body = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": namespace,
            },
        }

        try:
            self.core.create_namespace(body=body)
        except ApiException as exc:
            raise DeploymentError(
                f"Failed to create namespace {namespace}: {self._format_api_error(exc)}"
            ) from exc

    def get_node_by_k3s_name(self, k3s_node_name: str) -> Node | None:
        for node in self.inventory.nodes:
            if node.kubernetes_settings is None:
                continue

            if node.kubernetes_settings.k3s_node_name == k3s_node_name:
                return node

        return None

    def default_platform(self) -> MachinePlatform:
        if not self.inventory.nodes:
            raise DeploymentError("No inventory nodes available")

        return self.inventory.nodes[0].machine_settings.platform

    def update_service_discovery_metadata(
        self,
        *,
        service_name: str,
        namespace: str | None = None,
        model: str | None = None,
        capabilities: list[str] | None = None,
        modalities: list[str] | None = None,
        extra_annotations: dict[str, str] | None = None,
    ) -> dict[str, str]:
        namespace = namespace or self.namespace

        annotations: dict[str, str] = {}

        if model is not None:
            annotations["orin.ai/model"] = model

        if capabilities is not None:
            annotations["orin.ai/capabilities"] = json.dumps(capabilities)

        if modalities is not None:
            annotations["orin.ai/modalities"] = json.dumps(modalities)

        if extra_annotations:
            annotations.update(extra_annotations)

        if not annotations:
            raise ValueError("No discovery metadata provided")

        body = {
            "metadata": {
                "annotations": annotations,
            }
        }

        try:
            self.core.patch_namespaced_service(
                name=service_name,
                namespace=namespace,
                body=body,
            )
        except ApiException as exc:
            raise DeploymentError(
                f"Failed to patch service metadata {namespace}/{service_name}: "
                f"{self._format_api_error(exc)}"
            ) from exc

        return annotations

    def resolve_pod_placement(
        self,
        *,
        role: PodAppKind,
        explicit_node_alias: str | None = None,
        platform: MachinePlatform | None = None,
    ) -> PlacementDecision:
        if explicit_node_alias is not None:
            node = self.inventory.getNode(explicit_node_alias)

            if node is None:
                raise DeploymentError(f"Unknown node alias: {explicit_node_alias}")

            if node.kubernetes_settings is None:
                raise DeploymentError(
                    f"Node has no Kubernetes settings: {explicit_node_alias}"
                )

            # Optional but useful: verify the node has actually joined Kubernetes.
            self._read_k8s_node_or_raise(node.kubernetes_settings.k3s_node_name)

            return PlacementDecision(
                node_name=node.kubernetes_settings.k3s_node_name,
                platform=node.machine_settings.platform,
                node_selector={
                    "kubernetes.io/hostname": node.kubernetes_settings.k3s_node_name,
                },
                affinity=None,
            )

        resolved_platform = platform or self.default_platform()

        # This selector is not user placement. It is compatibility placement:
        # the image tag is platform-specific, so the pod must land on a matching platform.
        node_selector = {
            **backend_base_selector(),
            **platform_selector(resolved_platform),
        }

        return PlacementDecision(
            node_name=None,
            platform=resolved_platform,
            node_selector=node_selector,
            affinity=preferred_role_affinity(role),
        )

    def _get_inventory_node_labels(self, node: Node) -> dict[str, str]:
        if node.kubernetes_settings is None:
            return {}

        try:
            k8s_node = self.core.read_node(
                name=node.kubernetes_settings.k3s_node_name,
            )
        except ApiException as exc:
            raise DeploymentError(
                f"Failed to read Kubernetes node "
                f"{node.kubernetes_settings.k3s_node_name}: "
                f"{self._format_api_error(exc)}"
            ) from exc

        return k8s_node.metadata.labels or {}

    def _nodes_matching_labels(
        self,
        required: dict[str, str],
    ) -> list[Node]:
        matches: list[Node] = []

        for node in self.inventory.nodes:
            if node.kubernetes_settings is None:
                continue

            labels = self._get_inventory_node_labels(node)

            if labels_match(labels, required):
                matches.append(node)

        matches.sort(key=lambda n: n.alias)
        return matches

    def _create_pvc_if_missing(
        self,
        *,
        namespace: str,
        name: str,
        body: dict,
    ) -> None:
        try:
            self.core.read_namespaced_persistent_volume_claim(
                name=name,
                namespace=namespace,
            )
            return
        except ApiException as exc:
            if exc.status != 404:
                raise DeploymentError(
                    f"Failed to read PVC {namespace}/{name}: {self._format_api_error(exc)}"
                ) from exc

        try:
            self.core.create_namespaced_persistent_volume_claim(
                namespace=namespace,
                body=body,
            )
        except ApiException as exc:
            raise DeploymentError(
                f"Failed to create PVC {namespace}/{name}: {self._format_api_error(exc)}"
            ) from exc

    def _upsert_deployment(
        self,
        *,
        namespace: str,
        name: str,
        body: dict,
    ) -> None:
        try:
            self.apps.read_namespaced_deployment(
                name=name,
                namespace=namespace,
            )
        except ApiException as exc:
            if exc.status != 404:
                raise DeploymentError(
                    f"Failed to read deployment {namespace}/{name}: "
                    f"{self._format_api_error(exc)}"
                ) from exc

            try:
                self.apps.create_namespaced_deployment(
                    namespace=namespace,
                    body=body,
                )
                return
            except ApiException as create_exc:
                raise DeploymentError(
                    f"Failed to create deployment {namespace}/{name}: "
                    f"{self._format_api_error(create_exc)}"
                ) from create_exc

        try:
            self.apps.patch_namespaced_deployment(
                name=name,
                namespace=namespace,
                body=body,
            )
        except ApiException as exc:
            raise DeploymentError(
                f"Failed to patch deployment {namespace}/{name}: "
                f"{self._format_api_error(exc)}"
            ) from exc

    def _upsert_service(
        self,
        *,
        namespace: str,
        name: str,
        body: dict,
    ) -> None:
        try:
            self.core.read_namespaced_service(
                name=name,
                namespace=namespace,
            )
        except ApiException as exc:
            if exc.status != 404:
                raise DeploymentError(
                    f"Failed to read service {namespace}/{name}: "
                    f"{self._format_api_error(exc)}"
                ) from exc

            try:
                self.core.create_namespaced_service(
                    namespace=namespace,
                    body=body,
                )
                return
            except ApiException as create_exc:
                raise DeploymentError(
                    f"Failed to create service {namespace}/{name}: "
                    f"{self._format_api_error(create_exc)}"
                ) from create_exc

        try:
            self.core.patch_namespaced_service(
                name=name,
                namespace=namespace,
                body=body,
            )
        except ApiException as exc:
            raise DeploymentError(
                f"Failed to patch service {namespace}/{name}: "
                f"{self._format_api_error(exc)}"
            ) from exc

    def _read_service_if_exists(
        self,
        *,
        namespace: str,
        name: str,
    ):
        try:
            return self.core.read_namespaced_service(
                name=name,
                namespace=namespace,
            )
        except ApiException as exc:
            if exc.status == 404:
                return None

            raise DeploymentError(
                f"Failed to read service {namespace}/{name}: "
                f"{self._format_api_error(exc)}"
            ) from exc

    def _read_k8s_node_or_raise(self, k3s_node_name: str):
        try:
            return self.core.read_node(name=k3s_node_name)
        except ApiException as exc:
            if exc.status == 404:
                raise DeploymentError(
                    f"Node is not installed or has not joined Kubernetes yet: {k3s_node_name}"
                ) from exc

            raise DeploymentError(
                f"Failed to read Kubernetes node {k3s_node_name}: "
                f"{self._format_api_error(exc)}"
            ) from exc

    def _list_pods_for_app(
        self,
        *,
        namespace: str,
        app_name: str,
    ) -> list[dict]:
        pods = self.core.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app={app_name}",
        )

        return [
            {
                "name": pod.metadata.name or "-",
                "phase": pod.status.phase or "-",
                "ready": self._pod_is_ready(pod),
                "node": pod.spec.node_name or "-",
            }
            for pod in pods.items
        ]
