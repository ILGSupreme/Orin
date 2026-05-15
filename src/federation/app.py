# src/federation/app.py

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from federation.capabilities import CapabilityError, CapabilityService
from federation.cortex_bridge import CortexBridge, CortexBridgeError
import httpx
from fastapi import FastAPI, HTTPException, Request, status

from common.system import configuration

from .envelopes import (
    EnvelopeError,
    validate_and_verify_envelope,
)
from .identity import (
    IdentityError,
    canonical_json_bytes,
    load_or_create_cluster_identity,
    require_valid_signature,
)
from federation.join_tokens import JoinTokenError, JoinTokenService
from .members import MemberError, MemberService
from .models import (
    CreateJoinTokenRequest,
    CreateJoinTokenResponse,
    CreateNetworkRequest,
    CreateNetworkResponse,
    FederatedWorkEnvelope,
    FederationWorkRecord,
    GetFederatedWorkResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    JoinNetworkRequest,
    JoinNetworkResponse,
    PublishCapabilitiesRequest,
    PublishCapabilitiesResponse,
    SubmitFederatedWorkResponse,
)
from .network_registry import NetworkRegistry, NetworkRegistryError
from .policy import (
    FederationPolicyError,
    check_capability_publish_allowed,
    check_federated_work_allowed,
    check_result_polling_allowed,
    sanitize_packet_for_cortex,
)
from .settings import FederationSettings, get_settings
from .storage import LocalNonceStore, MemoryFederationStorage


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    configuration.load_configuration_file("federation")

    settings = get_settings()

    app.state.internal_http = httpx.AsyncClient(
        timeout=httpx.Timeout(180.0, connect=2.0),
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        ),
        verify=False,
        trust_env=False,
    )

    app.state.external_http = httpx.AsyncClient(
        timeout=30.0,
        verify=True,
        trust_env=True,
        follow_redirects=True,
    )

    storage = MemoryFederationStorage(
        http=app.state.internal_http,
        settings=settings,
    )

    app.state.settings = settings
    app.state.storage = storage
    app.state.identity = load_or_create_cluster_identity(settings)
    app.state.nonce_store = LocalNonceStore(settings=settings)

    app.state.network_registry = NetworkRegistry(
        storage=storage,
        settings=settings,
    )

    app.state.join_tokens = JoinTokenService(storage=storage)
    app.state.members = MemberService(storage=storage)
    app.state.capabilities = CapabilityService(
        http=app.state.internal_http,
        settings=settings,
    )
    app.state.cortex_bridge = CortexBridge(
        http=app.state.internal_http,
        settings=settings,
    )

    try:
        yield
    finally:
        await app.state.internal_http.aclose()
        await app.state.external_http.aclose()


app = FastAPI(
    title="Orin Federation",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def settings(request: Request) -> FederationSettings:
    return request.app.state.settings


def storage(request: Request) -> MemoryFederationStorage:
    return request.app.state.storage


def network_registry(request: Request) -> NetworkRegistry:
    return request.app.state.network_registry


def join_tokens(request: Request) -> JoinTokenService:
    return request.app.state.join_tokens


def members(request: Request) -> MemberService:
    return request.app.state.members


def nonce_store(request: Request) -> LocalNonceStore:
    return request.app.state.nonce_store

def capabilities(request: Request) -> CapabilityService:
    return request.app.state.capabilities

def cortex_bridge(request: Request) -> CortexBridge:
    return request.app.state.cortex_bridge


# ---------------------------------------------------------------------------
# Basic service endpoints
# ---------------------------------------------------------------------------


@app.get("/live")
async def live() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "federation",
        "status": "live",
    }


@app.get("/ready")
async def ready(request: Request) -> dict[str, Any]:
    identity = request.app.state.identity
    cfg = settings(request)

    return {
        "ok": True,
        "service": "federation",
        "status": "ready",
        "cluster_id": identity.cluster_id,
        "protocol_version": cfg.protocol_version,
    }


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    identity = request.app.state.identity

    return {
        "ok": True,
        "service": "federation",
        "cluster_id": identity.cluster_id,
    }


# ---------------------------------------------------------------------------
# Network endpoints
# ---------------------------------------------------------------------------


@app.post("/federation/networks", response_model=CreateNetworkResponse)
async def create_network(
    request: Request,
    body: CreateNetworkRequest,
) -> CreateNetworkResponse:
    """
    Local/admin endpoint for creating a FederationNetwork.

    For now this is unprotected. Later this should be reachable only from the
    local command interface or protected by local admin policy.
    """

    try:
        identity = request.app.state.identity

        network = await network_registry(request).create_network(
            slug=body.slug,
            name=body.name,
            description=body.description,
            visibility=body.visibility,
            join_mode=body.join_mode,
            policy=body.policy,
            owner_cluster_id=identity.cluster_id,
        )

        return CreateNetworkResponse(
            ok=True,
            network=network,
        )
    except NetworkRegistryError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@app.get("/federation/networks")
async def list_networks(request: Request) -> dict[str, Any]:
    registry = network_registry(request)

    networks = await registry.list_public_networks()

    return {
        "ok": True,
        "networks": [registry.public_view(network).model_dump(mode="json") for network in networks],
    }


@app.post(
    "/federation/networks/{slug}/tokens",
    response_model=CreateJoinTokenResponse,
)
async def create_join_token(
    request: Request,
    slug: str,
    body: CreateJoinTokenRequest,
) -> CreateJoinTokenResponse:
    """
    Local/admin endpoint for creating a join token.

    The raw token is returned once and is never persisted.
    """

    try:
        identity = request.app.state.identity

        network = await network_registry(request).require_network_by_slug(
            slug,
            include_private=True,
        )

        created = await join_tokens(request).create_join_token(
            network_id=network.network_id,
            created_by_cluster_id=identity.cluster_id,
            scopes=body.scopes,
            max_uses=body.max_uses,
            expires_at=body.expires_at,
        )

        return CreateJoinTokenResponse(
            ok=True,
            token_id=created.token_record.token_id,
            token=created.raw_token,
            expires_at=created.token_record.expires_at,
        )
    except (NetworkRegistryError, JoinTokenError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@app.post(
    "/federation/networks/{slug}/join",
    response_model=JoinNetworkResponse,
)
async def join_network(
    request: Request,
    slug: str,
    body: JoinNetworkRequest,
) -> JoinNetworkResponse:
    """
    Token-gated join endpoint.

    The token is a bootstrap credential only. After this succeeds, the member
    is identified by cluster_id and public_key, and future requests must be
    signed with that member key.
    """

    try:
        registry = network_registry(request)

        network = await registry.require_network_by_slug(
            slug,
            include_private=True,
        )

        if network.join_mode != "token":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Network join mode is not token: {network.join_mode}",
            )

        await join_tokens(request).redeem_token(
            body.token,
            network_id=network.network_id,
        )

        member = await members(request).add_member(
            network_id=network.network_id,
            cluster_id=body.cluster_id,
            public_key=body.public_key,
            role="member",
            status="active",
            advertised_capabilities=body.advertised_capabilities,
        )

        await registry.refresh_member_count(network.network_id)

        return JoinNetworkResponse(
            ok=True,
            network_id=network.network_id,
            cluster_id=body.cluster_id,
            member=member,
        )

    except HTTPException:
        raise
    except (NetworkRegistryError, JoinTokenError, MemberError, IdentityError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@app.get("/federation/networks/{slug}")
async def get_network(
    request: Request,
    slug: str,
) -> dict[str, Any]:
    try:
        registry = network_registry(request)

        network = await registry.require_network_by_slug(
            slug,
            include_private=False,
        )

        return {
            "ok": True,
            "network": registry.public_view(network).model_dump(mode="json"),
        }

    except NetworkRegistryError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

@app.get("/federation/networks/{slug}/capabilities")
async def get_network_capabilities(
    request: Request,
    slug: str,
) -> dict[str, Any]:
    try:
        registry = network_registry(request)

        network = await registry.require_network_by_slug(
            slug,
            include_private=False,
        )

        safe_network = registry.public_view(network)

        return {
            "ok": True,
            "network_id": safe_network.network_id,
            "slug": safe_network.slug,
            "capabilities": [
                capability.model_dump(mode="json")
                for capability in safe_network.advertised_capabilities
            ],
        }

    except NetworkRegistryError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    
@app.post("/federation/networks/{slug}/capabilities/refresh")
async def refresh_network_capabilities(
    request: Request,
    slug: str,
) -> dict[str, Any]:
    try:
        registry = network_registry(request)

        network = await registry.require_network_by_slug(
            slug,
            include_private=True,
        )

        summarized = await capabilities(request).summarize_local_capabilities(
            network=network,
        )

        updated = await registry.update_advertised_capabilities(
            network.network_id,
            summarized,
        )

        return {
            "ok": True,
            "network_id": updated.network_id,
            "slug": updated.slug,
            "capabilities": [
                capability.model_dump(mode="json")
                for capability in updated.advertised_capabilities
            ],
        }

    except (NetworkRegistryError, CapabilityError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

# ---------------------------------------------------------------------------
# Member/private endpoints
# ---------------------------------------------------------------------------


@app.post(
    "/federation/networks/{network_id}/heartbeat",
    response_model=HeartbeatResponse,
)
async def heartbeat(
    request: Request,
    network_id: str,
    body: HeartbeatRequest,
) -> HeartbeatResponse:
    try:
        network = await network_registry(request).require_network(network_id)

        member = await members(request).require_active_member(
            network_id=network.network_id,
            cluster_id=body.cluster_id,
        )

        _verify_signed_request(
            network_id=network.network_id,
            public_key=member.public_key,
            body=body,
            request_settings=settings(request),
            request_nonce_store=nonce_store(request),
        )

        await members(request).update_last_seen(
            network_id=network.network_id,
            cluster_id=member.cluster_id,
        )

        return HeartbeatResponse(
            ok=True,
            network_id=network.network_id,
            cluster_id=member.cluster_id,
            server_time=utc_now(),
        )

    except (NetworkRegistryError, MemberError, IdentityError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@app.post(
    "/federation/networks/{network_id}/capabilities",
    response_model=PublishCapabilitiesResponse,
)
async def publish_capabilities(
    request: Request,
    network_id: str,
    body: PublishCapabilitiesRequest,
) -> PublishCapabilitiesResponse:
    try:
        network = await network_registry(request).require_network(network_id)

        check_capability_publish_allowed(network.policy)

        member = await members(request).require_active_member(
            network_id=network.network_id,
            cluster_id=body.cluster_id,
        )

        _verify_signed_request(
            network_id=network.network_id,
            public_key=member.public_key,
            body=body,
            request_settings=settings(request),
            request_nonce_store=nonce_store(request),
        )

        updated = await members(request).update_capabilities(
            network_id=network.network_id,
            cluster_id=member.cluster_id,
            capabilities=body.capabilities,
        )

        return PublishCapabilitiesResponse(
            ok=True,
            network_id=network.network_id,
            cluster_id=updated.cluster_id,
            accepted_count=len(updated.advertised_capabilities),
        )

    except (NetworkRegistryError, MemberError, IdentityError, FederationPolicyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@app.post(
    "/federation/networks/{network_id}/work",
    response_model=SubmitFederatedWorkResponse,
)
async def submit_federated_work(
    request: Request,
    network_id: str,
    envelope: FederatedWorkEnvelope,
) -> SubmitFederatedWorkResponse:
    """
    Accept a signed federated work envelope.

    Phase 1 validates membership, signature, nonce, and policy, then stores
    the work record as accepted. Real execution should be moved into
    cortex_bridge.py next.
    """

    try:
        cfg = settings(request)

        network = await network_registry(request).require_network(network_id)

        member = await members(request).require_active_member(
            network_id=network.network_id,
            cluster_id=envelope.origin_cluster_id,
        )

        validate_and_verify_envelope(
            envelope=envelope,
            public_key=member.public_key,
            expected_network_id=network.network_id,
            expected_origin_cluster_id=member.cluster_id,
            nonce_store=nonce_store(request),
            settings=cfg,
        )

        check_federated_work_allowed(
            network=network,
            member=member,
            packet=envelope.packet,
            settings=cfg,
        )

        sanitized_packet = sanitize_packet_for_cortex(
            packet=envelope.packet,
            network=network,
            member=member,
            request_id=envelope.request_id,
        )

        record = await cortex_bridge(request).submit_packet(
            network=network,
            member=member,
            request_id=envelope.request_id,
            packet=sanitized_packet,
        )

        await storage(request).put_work_record(record)

        return SubmitFederatedWorkResponse(
            ok=True,
            network_id=network.network_id,
            work_id=record.work_id,
            request_id=record.request_id,
            status=record.status,
        )

    except (
        NetworkRegistryError,
        MemberError,
        EnvelopeError,
        IdentityError,
        FederationPolicyError,
        CortexBridgeError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@app.get(
    "/federation/networks/{network_id}/work/{work_id}",
    response_model=GetFederatedWorkResponse,
)
async def get_federated_work(
    request: Request,
    network_id: str,
    work_id: str,
) -> GetFederatedWorkResponse:
    try:
        network = await network_registry(request).require_network(network_id)

        check_result_polling_allowed(network.policy)

        record = await storage(request).get_work_record(
            network_id=network.network_id,
            work_id=work_id,
        )

        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Federated work not found: {work_id}",
            )

        return GetFederatedWorkResponse(
            ok=True,
            network_id=network.network_id,
            work_id=record.work_id,
            status=record.status,
            result=record.result,
            error=record.error,
        )

    except HTTPException:
        raise
    except (NetworkRegistryError, FederationPolicyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _verify_signed_request(
    *,
    network_id: str,
    public_key: str,
    body: Any,
    request_settings: FederationSettings,
    request_nonce_store: LocalNonceStore,
) -> None:
    """
    Verify simple signed member requests such as heartbeat and capability publish.

    The signed payload includes the path network_id plus the request body
    excluding signature. This prevents replaying a valid signed body against
    another network endpoint.
    """

    issued_at = ensure_aware(body.issued_at)

    now = utc_now()
    max_future = now + timedelta(seconds=request_settings.allowed_clock_skew_seconds)
    min_issued = now - timedelta(
        seconds=request_settings.request_ttl_seconds
        + request_settings.allowed_clock_skew_seconds
    )

    if issued_at > max_future:
        raise IdentityError("Signed request issued_at is too far in the future")

    if issued_at < min_issued:
        raise IdentityError("Signed request has expired")

    if not request_nonce_store.check_and_remember(body.nonce):
        raise IdentityError("Signed request nonce has already been seen")

    payload = {
        "network_id": network_id,
        "body": body.model_dump(
            mode="json",
            exclude={"signature"},
        ),
    }

    require_valid_signature(
        public_key=public_key,
        payload=canonical_json_bytes(payload),
        signature=body.signature,
    )