# src/federation/envelopes.py

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from federation.identity import (
    ClusterIdentity,
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
    ttl_seconds: int = 300,
    protocol_version: str = "v1",
    target_cluster_id: str | None = None,
    request_id: str | None = None,
    nonce: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> FederatedWorkEnvelope:
    """
    Create and sign a FederatedWorkEnvelope.

    The signature covers all envelope fields except `signature`.
    """

    now = utc_now()
    ttl = ttl_seconds

    unsigned_envelope = FederatedWorkEnvelope(
        federation_version=protocol_version,
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


def verify_envelope_signature_body(
    *,
    body: dict[str, Any],
    public_key: str,
) -> None:
    """
    Verify envelope signature against the supplied member public key.

    This function only verifies cryptographic integrity. It does not check
    membership, role, policy, quotas, or work permissions.
    """

    signature = body.get("signature")
    if not isinstance(signature, str) or not signature:
        raise EnvelopeError("Missing envelope signature")

    require_valid_signature(
        public_key=public_key,
        payload=envelope_body_signing_bytes(body),
        signature=signature,
    )


def envelope_body_signing_payload(body: dict[str, Any]) -> dict[str, Any]:
    """
    Return the canonical payload used for signing and verification.

    Do not include the signature itself. Everything else is part of the signed
    envelope, including signature_algorithm.
    """

    return {key: value for key, value in body.items() if key != "signature"}


def envelope_body_signing_bytes(body: dict[str, Any]) -> bytes:
    return canonical_json_bytes(envelope_body_signing_payload(body))


def validate_envelope_timing(
    *,
    body: dict[str, Any],
    settings: FederationSettings | None = None,
    now: datetime | None = None,
) -> None:
    settings = settings or get_settings()

    issued_at = parse_datetime_field(
        body=body,
        field_name="issued_at",
    )
    expires_at = parse_datetime_field(
        body=body,
        field_name="expires_at",
    )

    current_time = ensure_aware(now or utc_now())

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


def validate_envelope_target(
    *,
    body: dict[str, Any],
    expected_target_cluster_id: str | None,
) -> None:
    target_cluster_id = body.get("target_cluster_id")

    if target_cluster_id is None:
        return

    if not isinstance(target_cluster_id, str) or not target_cluster_id:
        raise EnvelopeError("Invalid target_cluster_id")

    if (
        expected_target_cluster_id is not None
        and target_cluster_id != expected_target_cluster_id
    ):
        raise EnvelopeError(
            "Envelope target_cluster_id does not match this federation node"
        )


def validate_envelope_version(
    *,
    body: dict[str, Any],
    settings: FederationSettings | None = None,
) -> None:
    settings = settings or get_settings()

    federation_version: str | None = body.get("federation_version", None)
    if not isinstance(federation_version, str) or not federation_version:
        raise EnvelopeError("Missing envelope federation version")

    if federation_version != settings.protocol_version:
        raise EnvelopeVersionError(
            f"Unsupported federation version: {federation_version}"
        )


def validate_signature_algorithm(body: dict[str, Any]) -> None:
    signature_algorithm: str | None = body.get("signature_algorithm", None)
    if not isinstance(signature_algorithm, str) or not signature_algorithm:
        raise EnvelopeError("Missing envelope signature")

    if signature_algorithm != "ed25519":
        raise EnvelopeSignatureAlgorithmError(
            f"Unsupported signature algorithm: {signature_algorithm}"
        )


def validate_envelope_origin(
    *,
    body: dict[str, Any],
    expected_origin_cluster_id: str,
) -> None:
    origin_cluster_id: str | None = body.get("origin_cluster_id", None)
    if not isinstance(origin_cluster_id, str) or not origin_cluster_id:
        raise EnvelopeError("Missing envelope origin_cluster_id")

    if origin_cluster_id != expected_origin_cluster_id:
        raise EnvelopeIdentityMismatchError(
            "Envelope origin_cluster_id does not match expected member identity"
        )


def validate_envelope_network(
    *,
    body: dict[str, Any],
    expected_network_id: str,
) -> None:
    network_id: str | None = body.get("network_id", None)
    if not isinstance(network_id, str) or not network_id:
        raise EnvelopeError("Missing envelope network_id")

    if network_id != expected_network_id:
        raise EnvelopeIdentityMismatchError(
            "Envelope network_id does not match target network"
        )


def check_and_remember_nonce(
    *,
    body: dict[str, Any],
    nonce_store: LocalNonceStore,
) -> None:
    nonce: str | None = body.get("nonce", None)
    if not isinstance(nonce, str) or not nonce:
        raise EnvelopeError("Missing envelope nonce")
    if not nonce_store.check_and_remember(nonce):
        raise EnvelopeReplayError("Envelope nonce has already been seen")


def validate_and_verify_envelope(
    *,
    body: dict[str, Any],
    public_key: str,
    expected_network_id: str | None = None,
    expected_origin_cluster_id: str | None = None,
    expected_target_cluster_id: str | None = None,
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

    validate_envelope_version(body=body, settings=settings)
    validate_signature_algorithm(body=body)
    validate_envelope_timing(body=body, settings=settings)

    if expected_network_id is not None:
        validate_envelope_network(
            body=body,
            expected_network_id=expected_network_id,
        )

    if expected_origin_cluster_id is not None:
        validate_envelope_origin(
            body=body,
            expected_origin_cluster_id=expected_origin_cluster_id,
        )

    validate_envelope_target(
        body=body,
        expected_target_cluster_id=expected_target_cluster_id,
    )

    verify_envelope_signature_body(
        body=body,
        public_key=public_key,
    )

    if nonce_store is not None:
        check_and_remember_nonce(
            body=body,
            nonce_store=nonce_store,
        )


def parse_envelope(raw: dict[str, Any]) -> FederatedWorkEnvelope:
    try:
        return FederatedWorkEnvelope.model_validate(raw)
    except Exception as exc:
        raise EnvelopeError("Invalid FederatedWorkEnvelope") from exc
    
def parse_datetime_field(
    *,
    body: dict[str, Any],
    field_name: str,
) -> datetime:
    value = body.get(field_name)

    if not isinstance(value, str) or not value:
        raise EnvelopeError(f"Missing envelope {field_name}")

    try:
        # Accept normal ISO strings and common JSON/RFC3339 Z suffix.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EnvelopeError(f"Invalid envelope {field_name}") from exc

    return ensure_aware(parsed)


def envelope_to_json(envelope: FederatedWorkEnvelope) -> dict[str, Any]:
    return envelope.model_dump(mode="json")


def signed_payload_debug_hash(envelope: FederatedWorkEnvelope) -> str:
    """
    Returns a non-sensitive hash of the signed payload.

    Useful for logs/debugging without printing full packets or signatures.
    """

    import hashlib

    return hashlib.sha256(envelope_signing_bytes(envelope)).hexdigest()
