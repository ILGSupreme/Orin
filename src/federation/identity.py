# src/federation/identity.py

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .settings import FederationSettings, get_settings

_PUBLIC_KEY_PREFIX = "ed25519:"
_SIGNATURE_PREFIX = "ed25519:"


class IdentityError(RuntimeError):
    pass


class InvalidPublicKeyError(IdentityError):
    pass


class InvalidSignatureError(IdentityError):
    pass


class MissingPrivateKeyError(IdentityError):
    pass


@dataclass(slots=True)
class ClusterIdentity:
    cluster_id: str
    public_key: str
    private_key_path: Path
    public_key_path: Path
    private_key: Ed25519PrivateKey

    def sign_bytes(self, payload: bytes) -> str:
        return sign_bytes(self.private_key, payload)

    def sign_json(self, payload: Any) -> str:
        return sign_json(self.private_key, payload)


def load_or_create_cluster_identity_from_paths(
    *,
    private_key_path: Path,
    public_key_path: Path,
    cluster_id: str | None = None,
    allow_create: bool = True,
) -> ClusterIdentity:
    private_key_path = private_key_path.expanduser()
    public_key_path = public_key_path.expanduser()

    if private_key_path.exists():
        private_key = load_private_key(private_key_path)
    else:
        if public_key_path.exists():
            raise MissingPrivateKeyError(
                "Public key exists but private key is missing. "
                f"Refusing to create a new identity over existing public key: {public_key_path}"
            )

        if not allow_create:
            raise MissingPrivateKeyError(f"Private key not found: {private_key_path}")

        private_key = Ed25519PrivateKey.generate()
        save_private_key(private_key_path, private_key)

    public_key = public_key_to_string(private_key.public_key())

    if public_key_path.exists():
        existing_public_key = read_public_key_string(public_key_path)

        if existing_public_key != public_key:
            raise InvalidPublicKeyError(
                "Public key file does not match private key. "
                f"public_key_path={public_key_path}"
            )
    else:
        save_public_key(public_key_path, public_key)

    resolved_cluster_id = cluster_id or derive_cluster_id(public_key)

    return ClusterIdentity(
        cluster_id=resolved_cluster_id,
        public_key=public_key,
        private_key_path=private_key_path,
        public_key_path=public_key_path,
        private_key=private_key,
    )


def load_or_create_cluster_identity(
    settings: FederationSettings | None = None,
    *,
    allow_create: bool = True,
) -> ClusterIdentity:
    """
    Load or create the local Federation cluster identity.

    The configuration stores key paths only. The actual private/public key
    material lives in local files, or later in a mounted Kubernetes Secret.

    If settings.cluster_id is not configured, a stable cluster id is derived
    from the public key.
    """
    settings = settings or get_settings()

    return load_or_create_cluster_identity_from_paths(
        private_key_path=settings.resolved_private_key_path(),
        public_key_path=settings.resolved_public_key_path(),
        cluster_id=settings.cluster_id,
        allow_create=allow_create,
    )


def load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise IdentityError(f"Failed to read private key: {path}") from exc

    try:
        key = serialization.load_pem_private_key(
            raw,
            password=None,
        )
    except ValueError as exc:
        raise IdentityError(f"Invalid private key file: {path}") from exc

    if not isinstance(key, Ed25519PrivateKey):
        raise IdentityError(f"Private key is not Ed25519: {path}")

    return key


def save_private_key(path: Path, private_key: Ed25519PrivateKey) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    raw = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_bytes(raw)

    os.chmod(tmp_path, 0o600)
    tmp_path.replace(path)
    os.chmod(path, 0o600)


def read_public_key_string(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise IdentityError(f"Failed to read public key: {path}") from exc

    parse_public_key(value)
    return value


def save_public_key(path: Path, public_key: str) -> None:
    parse_public_key(public_key)

    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(public_key + "\n", encoding="utf-8")

    os.chmod(tmp_path, 0o644)
    tmp_path.replace(path)
    os.chmod(path, 0o644)


def public_key_to_string(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _PUBLIC_KEY_PREFIX + _b64url_encode(raw)


def parse_public_key(public_key: str) -> Ed25519PublicKey:
    if not public_key.startswith(_PUBLIC_KEY_PREFIX):
        raise InvalidPublicKeyError("Public key must start with 'ed25519:'")

    raw_value = public_key[len(_PUBLIC_KEY_PREFIX) :]

    try:
        raw = _b64url_decode(raw_value)
    except ValueError as exc:
        raise InvalidPublicKeyError("Invalid public key encoding") from exc

    if len(raw) != 32:
        raise InvalidPublicKeyError("Invalid Ed25519 public key length")

    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise InvalidPublicKeyError("Invalid Ed25519 public key") from exc


def derive_cluster_id(public_key: str) -> str:
    """
    Derive a stable cluster id from the public key.

    This avoids storing another identity value unless the config explicitly
    provides cluster_id.
    """

    digest = hashlib.sha256(public_key.encode("utf-8")).hexdigest()
    return f"orin-{digest[:32]}"


def sign_bytes(private_key: Ed25519PrivateKey, payload: bytes) -> str:
    signature = private_key.sign(payload)
    return _SIGNATURE_PREFIX + _b64url_encode(signature)


def sign_json(private_key: Ed25519PrivateKey, payload: Any) -> str:
    return sign_bytes(private_key, canonical_json_bytes(payload))


def verify_signature(
    *,
    public_key: str,
    payload: bytes,
    signature: str,
) -> bool:
    key = parse_public_key(public_key)
    raw_signature = parse_signature(signature)

    try:
        key.verify(raw_signature, payload)
    except InvalidSignature:
        return False

    return True


def require_valid_signature(
    *,
    public_key: str,
    payload: bytes,
    signature: str,
) -> None:
    if not verify_signature(
        public_key=public_key,
        payload=payload,
        signature=signature,
    ):
        raise InvalidSignatureError("Invalid signature")


def parse_signature(signature: str) -> bytes:
    if not signature.startswith(_SIGNATURE_PREFIX):
        raise InvalidSignatureError("Signature must start with 'ed25519:'")

    raw_value = signature[len(_SIGNATURE_PREFIX) :]

    try:
        raw = _b64url_decode(raw_value)
    except ValueError as exc:
        raise InvalidSignatureError("Invalid signature encoding") from exc

    if len(raw) != 64:
        raise InvalidSignatureError("Invalid Ed25519 signature length")

    return raw


def canonical_json_bytes(payload: Any) -> bytes:
    """
    Stable JSON serialization for signing.

    Used by envelopes.py so both sender and receiver sign/verify the exact
    same logical payload.
    """

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
