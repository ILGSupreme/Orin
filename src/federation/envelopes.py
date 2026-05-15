# src/federation/envelopes.py

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from federation.identity import (
    ClusterIdentity,
    InvalidSignatureError,
    canonical_json_bytes,
    require_valid_signature,
)
from .models import FederatedWorkEnvelope
from .settings import FederationSettings, get_settings
from .storage import LocalNonceStore


class EnvelopeError(RuntimeError):
    pass


class EnvelopeExpiredError(EnvelopeError):
    pass


class EnvelopeIssuedInFutureError(EnvelopeError):
    pass


class EnvelopeReplayError(EnvelopeError):
    pass


class EnvelopeVersionError(EnvelopeError):
    pass


class EnvelopeSignatureAlgorithmError(EnvelopeError):
    pass


class EnvelopeIdentityMismatchError(EnvelopeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def create_envelope(
    *,
    identity: ClusterIdentity,
    network_id: str,
    packet: dict[str, Any],
    target_cluster_id: str | None = None,
    request_id: str | None = None,
    nonce: str | None = None,
    ttl_seconds: int | None = None,
    metadata: dict[str, Any] | None = None,
    settings: FederationSettings | None = None,
) -> FederatedWorkEnvelope:
    """
    Create and sign a FederatedWorkEnvelope.

    The signature covers all envelope fields except `signature`.
    """

    settings = settings or get_settings()

    now = utc_now()
    ttl = ttl_seconds or settings.request_ttl_seconds

    unsigned_envelope = FederatedWorkEnvelope(
        federation_version=settings.protocol_version,
        network_id=network_id,
        origin_cluster_id=identity.cluster_id,
        target_cluster_id=target_cluster_id,
        request_id=request_id or str(uuid4()),
        issued_at=now,
        expires_at=now + timedelta(seconds=ttl),
        nonce=nonce or str(uuid4()),
        packet=packet,
        signature="",
        signature_algorithm="ed25519",
        metadata=dict(metadata or {}),
    )

    payload = envelope_signing_payload(unsigned_envelope)
    signature = identity.sign_json(payload)

    return unsigned_envelope.model_copy(
        update={
            "signature": signature,
        }
    )


def envelope_signing_payload(envelope: FederatedWorkEnvelope) -> dict[str, Any]:
    """
    Return the canonical payload used for signing and verification.

    Do not include the signature itself. Everything else is part of the signed
    envelope, including signature_algorithm.
    """

    return envelope.model_dump(
        mode="json",
        exclude={"signature"},
    )


def envelope_signing_bytes(envelope: FederatedWorkEnvelope) -> bytes:
    return canonical_json_bytes(envelope_signing_payload(envelope))


def verify_envelope_signature(
    *,
    envelope: FederatedWorkEnvelope,
    public_key: str,
) -> None:
    """
    Verify envelope signature against the supplied member public key.

    This function only verifies cryptographic integrity. It does not check
    membership, role, policy, quotas, or work permissions.
    """

    require_valid_signature(
        public_key=public_key,
        payload=envelope_signing_bytes(envelope),
        signature=envelope.signature,
    )


def validate_envelope_timing(
    envelope: FederatedWorkEnvelope,
    *,
    settings: FederationSettings | None = None,
    now: datetime | None = None,
) -> None:
    settings = settings or get_settings()

    current_time = now or utc_now()
    current_time = ensure_aware(current_time)

    issued_at = ensure_aware(envelope.issued_at)
    expires_at = ensure_aware(envelope.expires_at)

    max_future_time = current_time + timedelta(
        seconds=settings.allowed_clock_skew_seconds
    )

    if issued_at > max_future_time:
        raise EnvelopeIssuedInFutureError("Envelope issued_at is too far in the future")

    if expires_at <= current_time:
        raise EnvelopeExpiredError("Envelope has expired")

    max_allowed_expiry = issued_at + timedelta(
        seconds=settings.request_ttl_seconds + settings.allowed_clock_skew_seconds
    )

    if expires_at > max_allowed_expiry:
        raise EnvelopeExpiredError("Envelope expiry exceeds allowed request TTL")


def validate_envelope_version(
    envelope: FederatedWorkEnvelope,
    *,
    settings: FederationSettings | None = None,
) -> None:
    settings = settings or get_settings()

    if envelope.federation_version != settings.protocol_version:
        raise EnvelopeVersionError(
            "Unsupported federation version: "
            f"{envelope.federation_version}"
        )


def validate_signature_algorithm(envelope: FederatedWorkEnvelope) -> None:
    if envelope.signature_algorithm != "ed25519":
        raise EnvelopeSignatureAlgorithmError(
            f"Unsupported signature algorithm: {envelope.signature_algorithm}"
        )


def validate_envelope_origin(
    *,
    envelope: FederatedWorkEnvelope,
    expected_origin_cluster_id: str,
) -> None:
    if envelope.origin_cluster_id != expected_origin_cluster_id:
        raise EnvelopeIdentityMismatchError(
            "Envelope origin_cluster_id does not match expected member identity"
        )


def validate_envelope_network(
    *,
    envelope: FederatedWorkEnvelope,
    expected_network_id: str,
) -> None:
    if envelope.network_id != expected_network_id:
        raise EnvelopeIdentityMismatchError(
            "Envelope network_id does not match target network"
        )


def check_and_remember_nonce(
    envelope: FederatedWorkEnvelope,
    *,
    nonce_store: LocalNonceStore,
) -> None:
    if not nonce_store.check_and_remember(envelope.nonce):
        raise EnvelopeReplayError("Envelope nonce has already been seen")


def validate_and_verify_envelope(
    *,
    envelope: FederatedWorkEnvelope,
    public_key: str,
    expected_network_id: str | None = None,
    expected_origin_cluster_id: str | None = None,
    nonce_store: LocalNonceStore | None = None,
    settings: FederationSettings | None = None,
) -> None:
    """
    Validate and verify a received envelope.

    This performs:
      - federation version check
      - signature algorithm check
      - network check, if provided
      - origin cluster check, if provided
      - expiry / issued_at check
      - signature verification
      - nonce replay check, if nonce_store is provided

    It does not perform:
      - membership lookup
      - network policy enforcement
      - member permission enforcement
      - WorkPacket validation
      - quota checks
    """

    validate_envelope_version(envelope, settings=settings)
    validate_signature_algorithm(envelope)
    validate_envelope_timing(envelope, settings=settings)

    if expected_network_id is not None:
        validate_envelope_network(
            envelope=envelope,
            expected_network_id=expected_network_id,
        )

    if expected_origin_cluster_id is not None:
        validate_envelope_origin(
            envelope=envelope,
            expected_origin_cluster_id=expected_origin_cluster_id,
        )

    verify_envelope_signature(
        envelope=envelope,
        public_key=public_key,
    )

    if nonce_store is not None:
        check_and_remember_nonce(
            envelope,
            nonce_store=nonce_store,
        )


def parse_envelope(raw: dict[str, Any]) -> FederatedWorkEnvelope:
    try:
        return FederatedWorkEnvelope.model_validate(raw)
    except Exception as exc:
        raise EnvelopeError("Invalid FederatedWorkEnvelope") from exc


def envelope_to_json(envelope: FederatedWorkEnvelope) -> dict[str, Any]:
    return envelope.model_dump(mode="json")


def signed_payload_debug_hash(envelope: FederatedWorkEnvelope) -> str:
    """
    Returns a non-sensitive hash of the signed payload.

    Useful for logs/debugging without printing full packets or signatures.
    """

    import hashlib

    return hashlib.sha256(envelope_signing_bytes(envelope)).hexdigest()