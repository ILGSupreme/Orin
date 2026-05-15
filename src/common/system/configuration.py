# src/common/configuration.py

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, overload, cast

from pydantic import BaseModel, Field, field_validator


ServiceFileType = Literal["cortex", "federation"]

ImagePullPolicy = Literal["IfNotPresent", "Always", "Never"]
RuntimeClassName = Literal["nvidia", "runc"]
ConfigurationPlatform = Literal["amd64", "arm64", "jetson"]

FederationVisibility = Literal["public", "unlisted", "private"]
JoinMode = Literal["token", "approval", "open", "closed"]


class ConfigurationError(RuntimeError):
    pass


class ImageConfiguration(BaseModel):
    registry: str = ""
    version: str = ""


class DeploymentConfiguration(BaseModel):
    default_port: int = 8080
    cortex_node_port: int = 30080

    pvc_size: str = "30Gi"
    pvc_storage_class_name: str = "local-path"
    pvc_mount_path: str = "/models/huggingface"

    image_pull_policy: ImagePullPolicy = "Always"
    runtime_class_name: RuntimeClassName = "nvidia"

    cortex_service_account_name: str = "cortex"


class CortexConfiguration(BaseModel):
    namespace: str = "orin"

    alias: str = "orin"
    host: str = "orin"
    ip: str = "127.0.0.1"

    ssh_key: str = "/data/ssh/id_ed25519"
    user: str = "ubuntu"

    port: int = 8080

    nvidia_gpu: bool = False
    platform: ConfigurationPlatform = "amd64"

    k3s_node_name: str = "orin"
    in_cluster: bool = True

    images: ImageConfiguration = Field(default_factory=ImageConfiguration)
    deployment: DeploymentConfiguration = Field(default_factory=DeploymentConfiguration)


class FederationConfiguration(BaseModel):
    app_name: str = "orin-federation"
    protocol_version: str = "v1"

    host: str = "0.0.0.0"
    port: int = 8080

    cluster_id: str | None = None

    data_dir: Path = Path("/data/federation")

    # Public URL for other Federation nodes.
    # This should not be confused with internal Cortex URLs.
    public_base_url: str | None = None

    # Internal Cortex target used by Federation when it submits accepted work.
    # Never expose this externally.
    cortex_base_url: str = "http://cortex-service:8080"
    cortex_work_path: str = "/work"
    cortex_work_result_path: str = "/work/{work_id}"

    private_key_path: Path | None = None
    public_key_path: Path | None = None

    default_network_visibility: FederationVisibility = "public"
    default_join_mode: JoinMode = "token"

    request_ttl_seconds: int = 300
    allowed_clock_skew_seconds: int = 60
    nonce_ttl_seconds: int = 600

    max_request_bytes: int = 1_000_000

    enable_remote_work_submission: bool = True
    enable_capability_publish: bool = True
    enable_member_heartbeat: bool = True

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_data_dir(cls, value: str | Path) -> Path:
        return Path(value).expanduser().resolve()

    @field_validator("public_base_url", "cortex_base_url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None

        value = value.rstrip("/")

        if not value.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")

        return value

    @field_validator("cortex_work_path", "cortex_work_result_path")
    @classmethod
    def _path_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("path must start with '/'")
        return value

    def cortex_work_url(self) -> str:
        return f"{self.cortex_base_url}{self.cortex_work_path}"

    def cortex_work_result_url(self, work_id: str) -> str:
        path = self.cortex_work_result_path.format(work_id=work_id)
        return f"{self.cortex_base_url}{path}"


ConfigurationFile = CortexConfiguration | FederationConfiguration


DEFAULT_CONFIGURATION_PATHS: dict[ServiceFileType, Path] = {
    "cortex": Path("/data/configuration"),
    "federation": Path("/data/federation/configuration"),
}


_CONFIGURATION_FILE: ConfigurationFile | None = None
_CONFIGURATION_SERVICE_TYPE: ServiceFileType | None = None


def is_loaded() -> bool:
    return _CONFIGURATION_FILE is not None


def assert_loaded() -> None:
    if _CONFIGURATION_FILE is None:
        raise ConfigurationError("Configuration file has not been loaded")


def load_configuration(
    configuration_path: str | Path,
) -> dict[str, Any]:
    config_path = Path(configuration_path)

    if not config_path.is_file():
        raise ConfigurationError(f"Configuration file not found: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as file:
            content = json.load(file)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"Invalid JSON in configuration file: {config_path}"
        ) from exc
    except OSError as exc:
        raise ConfigurationError(
            f"Failed to read configuration file: {config_path}"
        ) from exc

    if not isinstance(content, dict):
        raise ConfigurationError(
            f"Configuration file must contain a JSON object: {config_path}"
        )

    return content


def load_configuration_file(
    service_type: ServiceFileType,
    configuration_path: str | Path | None = None,
) -> ConfigurationFile:
    global _CONFIGURATION_FILE
    global _CONFIGURATION_SERVICE_TYPE

    config_path = (
        Path(configuration_path)
        if configuration_path is not None
        else DEFAULT_CONFIGURATION_PATHS[service_type]
    )

    raw_config = load_configuration(config_path)

    match service_type:
        case "cortex":
            config = CortexConfiguration.model_validate(raw_config)

        case "federation":
            config = FederationConfiguration.model_validate(raw_config)

        case _:
            raise ConfigurationError(f"Unsupported service type: {service_type}")

    _CONFIGURATION_FILE = config
    _CONFIGURATION_SERVICE_TYPE = service_type

    return config


@overload
def get_configuration(service_type: Literal["cortex"]) -> CortexConfiguration:
    ...


@overload
def get_configuration(service_type: Literal["federation"]) -> FederationConfiguration:
    ...


@overload
def get_configuration(service_type: None = None) -> ConfigurationFile:
    ...


def get_configuration(
    service_type: ServiceFileType | None = None,
) -> ConfigurationFile:
    if _CONFIGURATION_FILE is None:
        raise ConfigurationError("Configuration file has not been loaded")

    if service_type is not None and _CONFIGURATION_SERVICE_TYPE != service_type:
        raise ConfigurationError(
            "Loaded configuration type mismatch: "
            f"expected {service_type}, got {_CONFIGURATION_SERVICE_TYPE}"
        )

    if service_type == "cortex":
        return cast(CortexConfiguration, _CONFIGURATION_FILE)

    if service_type == "federation":
        return cast(FederationConfiguration, _CONFIGURATION_FILE)

    return _CONFIGURATION_FILE
