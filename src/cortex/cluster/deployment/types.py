from dataclasses import dataclass, field
from typing import Literal

PodAppKind = Literal["cortex", "llm", "tool", "memory"]
StorageProfile = Literal["light", "medium", "high"]
MachinePlatform = Literal["amd64", "arm64", "jetson"]


@dataclass(slots=True)
class Machine:
    host: str
    ip: str
    platform: MachinePlatform
    nvidia_gpu: bool = False


@dataclass(slots=True)
class KubernetesConfig:
    k3s_node_name: str
    namespace: str
    data_dir: str = "/data/k3s"
    default_local_storage_path: str = "/data/k3s-storage"
    write_kubeconfig_mode: str = "644"
    disable: list[str] = field(default_factory=lambda: ["servicelb"])
    service_cidr: str = "10.43.0.0/16"
    cluster_cidr: str = "10.42.0.0/16"


@dataclass(slots=True)
class SSHConfig:
    default_user: str = "ubuntu"
    default_key: str | None = "/data/ssh/id_ed25519"
    port: int = 22
    auth_method: Literal["ssh_key", "password"] = "ssh_key"


@dataclass(slots=True)
class Node:
    alias: str
    machine_settings: Machine
    ssh_config: SSHConfig
    kubernetes_settings: KubernetesConfig | None = None

    def __eq__(self, other):
        return self.alias == other.alias


@dataclass(slots=True)
class Inventory:
    version: Literal[1] = 1  # this will change
    nodes: list[Node] = field(default_factory=list)

    def addNode(self, node: Node) -> None:
        if self.getNode(node.alias) is not None:
            raise ValueError(f"Node alias already exists: {node.alias}")

        self.nodes.append(node)

    def removeNode(self, alias: str) -> bool:
        before = len(self.nodes)
        self.nodes = [n for n in self.nodes if n.alias != alias]
        return len(self.nodes) != before

    def getNode(self, alias: str) -> Node | None:
        return next((n for n in self.nodes if n.alias == alias), None)


PodKind = Literal["cortex", "llm", "tool", "memory"]
ServiceType = Literal["ClusterIP", "NodePort", "LoadBalancer"]
ImagePullPolicy = Literal["Always", "IfNotPresent", "Never"]
K3sInstallMode = Literal["server", "agent"]


@dataclass(slots=True)
class PodPvcSpec:
    name: str
    mount_path: str
    size: str = "20Gi"
    storage_class_name: str = "local-path"
    access_modes: list[str] = field(default_factory=lambda: ["ReadWriteOnce"])


@dataclass(slots=True)
class PodProbeSpec:
    ready_path: str = "/ready"
    live_path: str = "/live"

    readiness_initial_delay_seconds: int = 20
    readiness_period_seconds: int = 5
    readiness_timeout_seconds: int = 5
    readiness_failure_threshold: int = 12

    liveness_initial_delay_seconds: int = 20
    liveness_period_seconds: int = 15
    liveness_timeout_seconds: int = 5
    liveness_failure_threshold: int = 8

    startup_path: str = "/ready"
    startup_period_seconds: int = 5
    startup_timeout_seconds: int = 5
    startup_failure_threshold: int = 120


@dataclass(slots=True)
class PodDiscoverySpec:
    kind: PodKind
    role: str

    visibility: str = "internal"
    health_path: str = "/health"
    work_path: str = "/work"
    models_path: str | None = None

    extra_annotations: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class PodDeploymentSpec:
    name: str
    image: str

    namespace: str = "orin"
    kind: PodKind = "llm"

    replicas: int = 1
    container_name: str | None = None
    port: int = 8080

    image_pull_policy: ImagePullPolicy = "Always"

    runtime_class_name: str | None = None
    service_account_name: str | None = None
    node_selector: dict[str, str] = field(default_factory=dict)
    affinity: dict | None = None

    env: dict[str, str] = field(default_factory=dict)
    env_from_config_maps: list[str] = field(default_factory=list)

    pvc: PodPvcSpec | None = None
    probes: PodProbeSpec = field(default_factory=PodProbeSpec)

    labels: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class PodServiceSpec:
    name: str

    namespace: str = "orin"
    app_name: str | None = None

    port: int = 8080
    target_port: int = 8080
    service_type: ServiceType = "ClusterIP"
    node_port: int | None = None

    discovery: PodDiscoverySpec | None = None
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class PlacementDecision:
    node_name: str | None
    platform: MachinePlatform
    node_selector: dict[str, str]
    affinity: dict | None = None


@dataclass(slots=True)
class K3sInstallResult:
    alias: str
    node_name: str
    mode: K3sInstallMode
    server_url: str | None
    stdout: str
    stderr: str
