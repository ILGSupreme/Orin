# src/federation/join_tokens.py

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence
from uuid import uuid4

from .models import JoinToken
from .storage import MemoryFederationStorage

_TOKEN_PREFIX = "orin_join_"


class JoinTokenError(RuntimeError):
    pass


class InvalidJoinTokenError(JoinTokenError):
    pass


class JoinTokenNotFoundError(JoinTokenError):
    pass


class JoinTokenRevokedError(JoinTokenError):
    pass


class JoinTokenExpiredError(JoinTokenError):
    pass


class JoinTokenExhaustedError(JoinTokenError):
    pass


class JoinTokenNetworkMismatchError(JoinTokenError):
    pass


@dataclass(slots=True)
class CreatedJoinToken:
    """
    Result returned when a join token is created.

    raw_token is returned once to the caller and must never be persisted.
    token_record contains only the hashed token.
    """

    raw_token: str
    token_record: JoinToken


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _build_raw_token(token_id: str, secret: str) -> str:
    return f"{_TOKEN_PREFIX}{token_id}.{secret}"


def _parse_token_id(raw_token: str) -> str:
    if not raw_token.startswith(_TOKEN_PREFIX):
        raise InvalidJoinTokenError("Invalid join token format")

    body = raw_token[len(_TOKEN_PREFIX) :]

    if "." not in body:
        raise InvalidJoinTokenError("Invalid join token format")

    token_id, secret = body.split(".", 1)

    if not token_id or not secret:
        raise InvalidJoinTokenError("Invalid join token format")

    return token_id


class JoinTokenService:
    """
    Join-token service.

    Access tokens are bootstrap credentials only. They are used to register
    a cluster as a NetworkMember. After that, future communication should use
    the member cluster keypair and signed requests.

    This service does not create NetworkMember records. That belongs in
    members.py or the join endpoint orchestration.
    """

    def __init__(self, storage: MemoryFederationStorage) -> None:
        self.storage = storage

    async def create_join_token(
        self,
        *,
        network_id: str,
        created_by_cluster_id: str,
        scopes: Sequence[str] | None = None,
        max_uses: int | None = 1,
        expires_at: datetime | None = None,
        expires_in_seconds: int | None = None
    ) -> CreatedJoinToken:
        if not network_id.strip():
            raise JoinTokenError("network_id cannot be empty")

        if not created_by_cluster_id.strip():
            raise JoinTokenError("created_by_cluster_id cannot be empty")

        if max_uses is not None and max_uses < 1:
            raise JoinTokenError("max_uses must be at least 1 or None")

        if expires_at is not None:
            expires_at = _ensure_aware(expires_at)
            if expires_at <= utc_now():
                raise JoinTokenError("expires_at must be in the future")
            
        if expires_at is None and expires_in_seconds is not None:
            expires_at = utc_now() + timedelta(seconds=expires_in_seconds)

        token_id = str(uuid4())
        secret = secrets.token_urlsafe(32)
        raw_token = _build_raw_token(token_id, secret)

        now = utc_now()

        token_record = JoinToken(
            token_id=token_id,
            network_id=network_id,
            token_hash=_hash_token(raw_token),
            scopes=list(scopes or []),
            max_uses=max_uses,
            used_count=0,
            expires_at=expires_at,
            created_by_cluster_id=created_by_cluster_id,
            status="active",
            created_at=now,
            updated_at=now,
        )

        await self.storage.put_join_token(token_record)

        return CreatedJoinToken(
            raw_token=raw_token,
            token_record=token_record,
        )

    async def get_join_token(self, token_id: str) -> JoinToken | None:
        return await self.storage.get_join_token(token_id)

    async def require_join_token(self, token_id: str) -> JoinToken:
        token = await self.storage.get_join_token(token_id)

        if token is None:
            raise JoinTokenNotFoundError(f"Join token not found: {token_id}")

        return token

    async def list_join_tokens(
        self,
        *,
        network_id: str | None = None,
        include_inactive: bool = True,
    ) -> list[JoinToken]:
        tokens = await self.storage.list_join_tokens()

        if network_id is not None:
            tokens = [token for token in tokens if token.network_id == network_id]

        if not include_inactive:
            tokens = [token for token in tokens if token.status == "active"]

        tokens.sort(key=lambda token: token.created_at)
        return tokens

    async def validate_raw_token(
        self,
        raw_token: str,
        *,
        network_id: str | None = None,
    ) -> JoinToken:
        token_id = _parse_token_id(raw_token)

        token = await self.storage.get_join_token(token_id)

        if token is None:
            raise JoinTokenNotFoundError("Join token not found")

        expected_hash = token.token_hash
        actual_hash = _hash_token(raw_token)

        if not secrets.compare_digest(expected_hash, actual_hash):
            raise InvalidJoinTokenError("Invalid join token")

        self._validate_token_state(token, network_id=network_id)

        return token

    async def redeem_token(
        self,
        raw_token: str,
        *,
        network_id: str,
    ) -> JoinToken:
        """
        Validates and consumes one use of a join token.

        This does not create a NetworkMember. The caller should use the
        returned token/network_id to create the member record after redemption.
        """

        token = await self.validate_raw_token(raw_token, network_id=network_id)

        token.used_count += 1
        token.updated_at = utc_now()

        await self.storage.put_join_token(token)

        return token

    async def revoke_token(self, token_id: str) -> JoinToken:
        token = await self.require_join_token(token_id)

        if token.status != "revoked":
            token.status = "revoked"
            token.updated_at = utc_now()
            await self.storage.put_join_token(token)

        return token

    async def expire_token(self, token_id: str) -> JoinToken:
        token = await self.require_join_token(token_id)

        if token.status != "expired":
            token.status = "expired"
            token.updated_at = utc_now()
            await self.storage.put_join_token(token)

        return token

    async def delete_token(self, token_id: str) -> None:
        await self.storage.delete_join_token(token_id)

    def _validate_token_state(
        self,
        token: JoinToken,
        *,
        network_id: str | None = None,
    ) -> None:
        if network_id is not None and token.network_id != network_id:
            raise JoinTokenNetworkMismatchError(
                "Join token does not belong to this network"
            )

        if token.status == "revoked":
            raise JoinTokenRevokedError("Join token has been revoked")

        if token.status == "expired":
            raise JoinTokenExpiredError("Join token has expired")

        if token.status != "active":
            raise InvalidJoinTokenError(f"Invalid join token status: {token.status}")

        if token.expires_at is not None:
            expires_at = _ensure_aware(token.expires_at)

            if expires_at <= utc_now():
                raise JoinTokenExpiredError("Join token has expired")

        if token.max_uses is not None and token.used_count >= token.max_uses:
            raise JoinTokenExhaustedError("Join token has no remaining uses")
