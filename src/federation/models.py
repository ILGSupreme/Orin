# src/federation/models.py

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


FederationVisibility = Literal["public", "unlisted", "private"]
JoinMode = Literal["token", "approval", "open", "closed"]

TokenStatus = Literal["active", "revoked", "expired"]

MemberRole = Literal["owner", "admin", "member", "guest"]
MemberStatus = Literal["active", "disabled", "revoked", "pending"]

FederationWorkStatus = Literal[
    "accepted",
    "running",
    "completed",
    "failed",
    "rejected",
]

FederationWorkType = Literal["llm", "tool", "cortex", "memory"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CapabilitySummary(BaseModel):
    """
    Sanitized public capability summary.

    This must never contain pod names, node IPs, internal service URLs,
    model paths, Kubernetes labels, SSH data, memory data, or deployment data.
    """

    work_type: str
    operations: list[str] = Field(default_factory=list)
    modalities: list[str] = Field(default_factory=list)

    max_context_hint: int | None = None
    max_result_tokens_hint: int | None = None

    availability_hint: str | None = None
    latency_hint: str | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)


class NetworkPolicy(BaseModel):
    """
    Default-deny policy for a FederationNetwork.

    Federation only allows work types and operations explicitly listed here.
    Anything else is rejected before it reaches Cortex.
    """

    allow_remote_work_submission: bool = True
    allow_remote_result_polling: bool = True
    allow_member_capability_publish: bool = True

    allowed_work_types: list[str] = Field(default_factory=lambda: ["llm", "tool"])

    allowed_operations: list[str] = Field(
        default_factory=lambda: [
            "chat",
            "summarize",
            "classify",
            "extract",
            "analyze",
            "search",
            "inspect",
        ]
    )

    max_payload_bytes: int = 1_000_000
    max_context_tokens: int = 4096
    max_result_tokens: int = 1024
    max_concurrent_jobs_per_member: int = 1

    expose_member_list: bool = False
    expose_exact_models: bool = False
    expose_runtime_metadata: bool = False


class FederationNetwork(BaseModel):
    network_id: str
    slug: str
    name: str
    description: str | None = None

    visibility: FederationVisibility = "public"
    join_mode: JoinMode = "token"

    owner_cluster_id: str
    policy: NetworkPolicy = Field(default_factory=NetworkPolicy)

    advertised_capabilities: list[CapabilitySummary] = Field(default_factory=list)
    member_count: int = 0

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    metadata: dict[str, Any] = Field(default_factory=dict)


class JoinToken(BaseModel):
    """
    Stored join-token record.

    token_hash must store only the hashed token.
    Never persist the raw token.
    """

    token_id: str
    network_id: str

    token_hash: str
    scopes: list[str] = Field(default_factory=list)

    max_uses: int | None = 1
    used_count: int = 0

    expires_at: datetime | None = None
    created_by_cluster_id: str

    status: TokenStatus = "active"

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class NetworkMember(BaseModel):
    network_id: str
    cluster_id: str
    public_key: str

    role: MemberRole = "member"
    status: MemberStatus = "active"

    allowed_work_types: list[str] = Field(default_factory=lambda: ["llm", "tool"])
    allowed_operations: list[str] = Field(
        default_factory=lambda: ["chat", "summarize", "analyze"]
    )

    joined_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime | None = None

    advertised_capabilities: list[CapabilitySummary] = Field(default_factory=list)

    metadata: dict[str, Any] = Field(default_factory=dict)


class FederatedWorkEnvelope(BaseModel):
    """
    Signed external wrapper around an internal WorkPacket-shaped payload.

    The packet is kept as dict[str, Any] here to avoid making the Federation
    model layer depend directly on Cortex/common imports. The bridge layer
    should validate it into the real WorkPacket type before submitting it.
    """

    federation_version: str = "v1"

    network_id: str
    origin_cluster_id: str
    target_cluster_id: str | None = None

    request_id: str
    issued_at: datetime
    expires_at: datetime
    nonce: str

    packet: dict[str, Any]

    signature: str
    signature_algorithm: str = "ed25519"

    metadata: dict[str, Any] = Field(default_factory=dict)


class FederationWorkRecord(BaseModel):
    """
    Local tracking record for federated work submitted through this node.
    """

    network_id: str
    work_id: str
    request_id: str

    origin_cluster_id: str
    target_cluster_id: str | None = None

    status: FederationWorkStatus = "accepted"

    result: dict[str, Any] | None = None
    error: str | None = None

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# API request / response DTOs
# ---------------------------------------------------------------------------


class CreateNetworkRequest(BaseModel):
    slug: str
    name: str
    description: str | None = None

    visibility: FederationVisibility = "public"
    join_mode: JoinMode = "token"

    policy: NetworkPolicy | None = None


class CreateNetworkResponse(BaseModel):
    ok: bool = True
    network: FederationNetwork


class CreateJoinTokenRequest(BaseModel):
    scopes: list[str] = Field(default_factory=list)
    max_uses: int | None = 1
    expires_at: datetime | None = None


class CreateJoinTokenResponse(BaseModel):
    ok: bool = True
    token_id: str

    # Returned once to the creator. Never store this raw value.
    token: str

    expires_at: datetime | None = None


class JoinNetworkRequest(BaseModel):
    token: str
    cluster_id: str
    public_key: str

    advertised_capabilities: list[CapabilitySummary] = Field(default_factory=list)


class JoinNetworkResponse(BaseModel):
    ok: bool = True
    network_id: str
    cluster_id: str
    member: NetworkMember


class HeartbeatRequest(BaseModel):
    cluster_id: str
    issued_at: datetime = Field(default_factory=utc_now)
    nonce: str
    signature: str


class HeartbeatResponse(BaseModel):
    ok: bool = True
    network_id: str
    cluster_id: str
    server_time: datetime = Field(default_factory=utc_now)


class PublishCapabilitiesRequest(BaseModel):
    cluster_id: str
    capabilities: list[CapabilitySummary] = Field(default_factory=list)

    issued_at: datetime = Field(default_factory=utc_now)
    nonce: str
    signature: str


class PublishCapabilitiesResponse(BaseModel):
    ok: bool = True
    network_id: str
    cluster_id: str
    accepted_count: int


class SubmitFederatedWorkResponse(BaseModel):
    ok: bool
    network_id: str
    work_id: str
    request_id: str
    status: FederationWorkStatus

    error: str | None = None


class GetFederatedWorkResponse(BaseModel):
    ok: bool
    network_id: str
    work_id: str
    status: FederationWorkStatus

    result: dict[str, Any] | None = None
    error: str | None = None
