"""Structured inventory records shared by Memory and Cortex.

Credentials are references only: never store private keys, passwords, or tokens.
"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress

Name = Annotated[str, Field(min_length=1, max_length=253, pattern=r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")]
Text = Annotated[str, Field(min_length=1, max_length=1024, pattern=r"^[^\s\x00-\x1f]+$")]


class InventoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InventoryMachine(InventoryModel):
    host: Text
    ip: IPvAnyAddress
    platform: Literal["amd64", "arm64", "jetson"]
    nvidia_gpu: bool = False


class InventorySSH(InventoryModel):
    default_user: Annotated[str, Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_-]*[$]?$", max_length=64)]
    default_key: Text | None = "/data/ssh/id_ed25519"
    port: int = Field(default=22, ge=1, le=65535)
    auth_method: Literal["ssh_key", "password"] = "ssh_key"


class InventoryKubernetes(InventoryModel):
    k3s_node_name: Name
    namespace: Name
    data_dir: Text = "/data/k3s"
    default_local_storage_path: Text = "/data/k3s-storage"
    write_kubeconfig_mode: str = Field(default="644", pattern=r"^[0-7]{3,4}$")
    disable: list[Text] = Field(default_factory=lambda: ["servicelb"])
    service_cidr: Text = "10.43.0.0/16"
    cluster_cidr: Text = "10.42.0.0/16"


class InventoryNode(InventoryModel):
    alias: Name
    machine_settings: InventoryMachine
    ssh_config: InventorySSH
    kubernetes_settings: InventoryKubernetes | None = None


class InventoryScope(InventoryModel):
    namespace: Name


class InventoryLookup(InventoryScope):
    alias: Name


class InventoryCreate(InventoryScope):
    node: InventoryNode


class InventoryUpdate(InventoryCreate):
    expected_revision: int = Field(ge=1)


class InventoryDelete(InventoryLookup):
    expected_revision: int = Field(ge=1)
