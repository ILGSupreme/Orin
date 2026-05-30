# src/federation/settings.py

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator

from common.system import configuration

FederationVisibility = Literal["public", "unlisted", "private"]
JoinMode = Literal["token", "approval", "open", "closed"]


class FederationSettings(BaseModel):
    """
    Runtime configuration for the Federation app.

    Persistent Federation data is stored through the Memory service.
    Local files are only for identity keys, nonce/cache data, and temporary state.

    This model is loaded from the Federation configuration file.
    It should not create identities, register networks, create tokens,
    connect to peers, or start transport services.
    """

    app_name: str = "orin-federation"
    protocol_version: str = "v1"

    host: str = "0.0.0.0"
    port: int = 8080

    # Stable identity is loaded/created by identity.py.
    # If cluster_id is not provided, identity.py may create and persist one.
    cluster_id: str | None = None

    data_dir: Path = Path("/data/federation")

    private_key_path: Path | None = None
    public_key_path: Path | None = None

    # Existing Memory service used for persistent Federation state:
    # networks, members, join-token records, capability records, and work records.
    memory_base_url: str = "http://memory-service:8080"
    memory_work_path: str = "/work"

    # Public URL where this Federation node can be reached by other nodes.
    # May be empty for local-only development.
    public_base_url: str | None = None

    # Internal Cortex URL. This must never be exposed in public federation metadata.
    cortex_base_url: str = "http://cortex-service:8080"
    cortex_work_path: str = "/work"
    cortex_work_result_path: str = "/work/{work_id}"

    default_network_visibility: FederationVisibility = "public"
    default_join_mode: JoinMode = "token"

    cortex_network_path: str = "/network"

    # Envelope/security limits.
    request_ttl_seconds: int = 300
    allowed_clock_skew_seconds: int = 60
    nonce_ttl_seconds: int = 600

    # Defensive API limit. NetworkPolicy still decides per-network limits.
    max_request_bytes: int = 1_000_000

    # Phase 1 feature switches.
    enable_remote_work_submission: bool = True
    enable_capability_publish: bool = True
    enable_member_heartbeat: bool = True

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_data_dir(cls, value: str | Path) -> Path:
        return Path(value).expanduser().resolve()

    @field_validator("private_key_path", "public_key_path", mode="before")
    @classmethod
    def _expand_key_path(cls, value: str | Path | None) -> Path | None:
        if value is None or value == "":
            return None
        return Path(value).expanduser().resolve()

    @field_validator(
        "memory_work_path",
        "cortex_work_path",
        "cortex_work_result_path",
        "cortex_network_path",
    )
    @classmethod
    def _path_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("path must start with '/'")
        return value

    @field_validator("public_base_url", "memory_base_url", "cortex_base_url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None

        value = value.rstrip("/")

        if not value.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")

        return value

    def resolved_private_key_path(self) -> Path:
        if self.private_key_path is not None:
            return self.private_key_path
        return self.data_dir / "identity" / "ed25519_private.key"

    def resolved_public_key_path(self) -> Path:
        if self.public_key_path is not None:
            return self.public_key_path
        return self.data_dir / "identity" / "ed25519_public.key"

    def memory_work_url(self) -> str:
        return f"{self.memory_base_url}{self.memory_work_path}"

    def cortex_work_url(self) -> str:
        return f"{self.cortex_base_url}{self.cortex_work_path}"

    def cortex_work_result_url(self, work_id: str) -> str:
        path = self.cortex_work_result_path.format(work_id=work_id)
        return f"{self.cortex_base_url}{path}"

    def cortex_network_url(self) -> str:
        return f"{self.cortex_base_url}{self.cortex_network_path}"


def load_settings_from_file() -> FederationSettings:
    cfg = configuration.get_configuration("federation")

    # Works if cfg is a Pydantic model.
    if isinstance(cfg, BaseModel):
        return FederationSettings.model_validate(cfg.model_dump())

    # Fallback if get_configuration ever returns a plain dict.
    return FederationSettings.model_validate(cfg)


@lru_cache(maxsize=1)
def get_settings() -> FederationSettings:
    settings = load_settings_from_file()

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.resolved_private_key_path().parent.mkdir(parents=True, exist_ok=True)
    settings.resolved_public_key_path().parent.mkdir(parents=True, exist_ok=True)

    return settings
