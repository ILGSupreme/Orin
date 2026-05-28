# src/federation/members.py

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from .models import CapabilitySummary, NetworkMember
from .storage import MemoryFederationStorage


class MemberError(RuntimeError):
    pass


class MemberNotFoundError(MemberError):
    pass


class MemberAlreadyExistsError(MemberError):
    pass


class InvalidMemberError(MemberError):
    pass


class MemberNotActiveError(MemberError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MemberService:
    """
    Network member service.

    This service owns NetworkMember records:
      - create member after successful join-token redemption
      - read/list members
      - update status
      - update heartbeat
      - update advertised capabilities
      - update per-member allowed work types and operations

    It does not validate join tokens. That belongs in join_tokens.py.
    It does not verify request signatures. That belongs in envelopes.py / identity.py.
    It does not refresh FederationNetwork.member_count. The endpoint/app layer should
    call NetworkRegistry.refresh_member_count() after member create/delete/status changes.
    """

    def __init__(self, storage: MemoryFederationStorage) -> None:
        self.storage = storage

    async def add_member(
        self,
        *,
        network_id: str,
        cluster_id: str,
        public_key: str,
        role: str = "member",
        status: str = "active",
        allowed_work_types: Sequence[str] | None = None,
        allowed_operations: Sequence[str] | None = None,
        advertised_capabilities: Sequence[CapabilitySummary] | None = None,
        metadata: dict[str, object] | None = None,
        replace_existing: bool = False,
    ) -> NetworkMember:
        self._validate_member_input(
            network_id=network_id,
            cluster_id=cluster_id,
            public_key=public_key,
        )

        existing = await self.storage.get_member(
            network_id=network_id,
            cluster_id=cluster_id,
        )

        if existing is not None and not replace_existing:
            raise MemberAlreadyExistsError(
                f"NetworkMember already exists: {network_id}:{cluster_id}"
            )

        now = utc_now()

        if existing is not None and replace_existing:
            member = existing
            member.public_key = public_key
            member.role = role
            member.status = status
            member.allowed_work_types = list(
                allowed_work_types or member.allowed_work_types
            )
            member.allowed_operations = list(
                allowed_operations or member.allowed_operations
            )
            member.advertised_capabilities = list(
                advertised_capabilities or member.advertised_capabilities
            )
            member.last_seen_at = now
            member.metadata.update(metadata or {})
            member.metadata["updated_at"] = now.isoformat()
        else:
            member = NetworkMember(
                network_id=network_id,
                cluster_id=cluster_id,
                public_key=public_key,
                role=role,
                status=status,
                allowed_work_types=list(allowed_work_types or ["llm", "tool"]),
                allowed_operations=list(
                    allowed_operations or ["chat", "summarize", "analyze"]
                ),
                joined_at=now,
                last_seen_at=now,
                advertised_capabilities=list(advertised_capabilities or []),
                metadata=dict(metadata or {}),
            )

        await self.storage.put_member(member)
        return member

    async def get_member(
        self,
        *,
        network_id: str,
        cluster_id: str,
    ) -> NetworkMember | None:
        return await self.storage.get_member(
            network_id=network_id,
            cluster_id=cluster_id,
        )

    async def require_member(
        self,
        *,
        network_id: str,
        cluster_id: str,
    ) -> NetworkMember:
        member = await self.get_member(
            network_id=network_id,
            cluster_id=cluster_id,
        )

        if member is None:
            raise MemberNotFoundError(
                f"NetworkMember not found: {network_id}:{cluster_id}"
            )

        return member

    async def require_active_member(
        self,
        *,
        network_id: str,
        cluster_id: str,
    ) -> NetworkMember:
        member = await self.require_member(
            network_id=network_id,
            cluster_id=cluster_id,
        )

        if member.status != "active":
            raise MemberNotActiveError(
                f"NetworkMember is not active: {network_id}:{cluster_id}"
            )

        return member

    async def list_members(
        self,
        *,
        network_id: str | None = None,
        include_inactive: bool = True,
    ) -> list[NetworkMember]:
        members = await self.storage.list_members(network_id)

        if not include_inactive:
            members = [member for member in members if member.status == "active"]

        members.sort(key=lambda member: (member.network_id, member.cluster_id))
        return members

    async def update_member_status(
        self,
        *,
        network_id: str,
        cluster_id: str,
        status: str,
    ) -> NetworkMember:
        member = await self.require_member(
            network_id=network_id,
            cluster_id=cluster_id,
        )

        member.status = status
        member.metadata["updated_at"] = utc_now().isoformat()

        await self.storage.put_member(member)
        return member

    async def disable_member(
        self,
        *,
        network_id: str,
        cluster_id: str,
    ) -> NetworkMember:
        return await self.update_member_status(
            network_id=network_id,
            cluster_id=cluster_id,
            status="disabled",
        )

    async def revoke_member(
        self,
        *,
        network_id: str,
        cluster_id: str,
    ) -> NetworkMember:
        return await self.update_member_status(
            network_id=network_id,
            cluster_id=cluster_id,
            status="revoked",
        )

    async def activate_member(
        self,
        *,
        network_id: str,
        cluster_id: str,
    ) -> NetworkMember:
        return await self.update_member_status(
            network_id=network_id,
            cluster_id=cluster_id,
            status="active",
        )

    async def update_last_seen(
        self,
        *,
        network_id: str,
        cluster_id: str,
        seen_at: datetime | None = None,
    ) -> NetworkMember:
        member = await self.require_active_member(
            network_id=network_id,
            cluster_id=cluster_id,
        )

        member.last_seen_at = seen_at or utc_now()
        member.metadata["updated_at"] = utc_now().isoformat()

        await self.storage.put_member(member)
        return member

    async def update_capabilities(
        self,
        *,
        network_id: str,
        cluster_id: str,
        capabilities: Sequence[CapabilitySummary],
    ) -> NetworkMember:
        member = await self.require_active_member(
            network_id=network_id,
            cluster_id=cluster_id,
        )

        member.advertised_capabilities = list(capabilities)
        member.last_seen_at = utc_now()
        member.metadata["updated_at"] = utc_now().isoformat()

        await self.storage.put_member(member)
        return member

    async def update_permissions(
        self,
        *,
        network_id: str,
        cluster_id: str,
        allowed_work_types: Sequence[str] | None = None,
        allowed_operations: Sequence[str] | None = None,
    ) -> NetworkMember:
        member = await self.require_member(
            network_id=network_id,
            cluster_id=cluster_id,
        )

        if allowed_work_types is not None:
            member.allowed_work_types = list(allowed_work_types)

        if allowed_operations is not None:
            member.allowed_operations = list(allowed_operations)

        member.metadata["updated_at"] = utc_now().isoformat()

        await self.storage.put_member(member)
        return member

    async def update_role(
        self,
        *,
        network_id: str,
        cluster_id: str,
        role: str,
    ) -> NetworkMember:
        member = await self.require_member(
            network_id=network_id,
            cluster_id=cluster_id,
        )

        member.role = role
        member.metadata["updated_at"] = utc_now().isoformat()

        await self.storage.put_member(member)
        return member

    async def delete_member(
        self,
        *,
        network_id: str,
        cluster_id: str,
    ) -> None:
        existing = await self.storage.get_member(
            network_id=network_id,
            cluster_id=cluster_id,
        )

        if existing is None:
            raise MemberNotFoundError(
                f"NetworkMember not found: {network_id}:{cluster_id}"
            )

        await self.storage.delete_member(
            network_id=network_id,
            cluster_id=cluster_id,
        )

    def member_can_submit(
        self,
        member: NetworkMember,
        *,
        work_type: str,
        operation: str,
    ) -> bool:
        return (
            member.status == "active"
            and work_type in member.allowed_work_types
            and operation in member.allowed_operations
        )

    def require_member_can_submit(
        self,
        member: NetworkMember,
        *,
        work_type: str,
        operation: str,
    ) -> None:
        if member.status != "active":
            raise MemberNotActiveError(
                f"NetworkMember is not active: {member.network_id}:{member.cluster_id}"
            )

        if work_type not in member.allowed_work_types:
            raise InvalidMemberError(
                f"Member is not allowed to submit work_type: {work_type}"
            )

        if operation not in member.allowed_operations:
            raise InvalidMemberError(
                f"Member is not allowed to submit operation: {operation}"
            )

    def _validate_member_input(
        self,
        *,
        network_id: str,
        cluster_id: str,
        public_key: str,
    ) -> None:
        if not network_id.strip():
            raise InvalidMemberError("network_id cannot be empty")

        if not cluster_id.strip():
            raise InvalidMemberError("cluster_id cannot be empty")

        if not public_key.strip():
            raise InvalidMemberError("public_key cannot be empty")
