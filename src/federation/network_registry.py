# src/federation/network_registry.py

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Sequence
from uuid import uuid4

from .models import CapabilitySummary, FederationNetwork, NetworkPolicy
from .settings import FederationSettings, FederationVisibility, JoinMode, get_settings
from .storage import MemoryFederationStorage


_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


class NetworkRegistryError(RuntimeError):
    pass


class NetworkAlreadyExistsError(NetworkRegistryError):
    pass


class NetworkNotFoundError(NetworkRegistryError):
    pass


class InvalidNetworkSlugError(NetworkRegistryError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_network_slug(slug: str) -> str:
    normalized = slug.strip().lower()

    if not _SLUG_PATTERN.fullmatch(normalized):
        raise InvalidNetworkSlugError(
            "Invalid network slug. Use 3-64 lowercase letters, numbers, "
            "and hyphens. It must start and end with a letter or number."
        )

    return normalized


class NetworkRegistry:
    """
    Local registry for FederationNetwork records.

    Persistent records are stored through MemoryFederationStorage.
    This class owns network creation, lookup, listing, and capability updates.
    It does not handle join tokens, members, signatures, or work routing.
    """

    def __init__(
        self,
        storage: MemoryFederationStorage,
        settings: FederationSettings | None = None,
    ) -> None:
        self.storage = storage
        self.settings = settings or get_settings()

    async def create_network(
        self,
        *,
        slug: str,
        name: str,
        owner_cluster_id: str,
        description: str | None = None,
        visibility: FederationVisibility | None = None,
        join_mode: JoinMode | None = None,
        policy: NetworkPolicy | None = None,
        advertised_capabilities: Sequence[CapabilitySummary] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> FederationNetwork:
        slug = validate_network_slug(slug)

        existing = await self.storage.get_network_by_slug(slug)
        if existing is not None:
            raise NetworkAlreadyExistsError(f"FederationNetwork already exists: {slug}")

        now = utc_now()

        network = FederationNetwork(
            network_id=str(uuid4()),
            slug=slug,
            name=name.strip(),
            description=description,
            visibility=visibility or self.settings.default_network_visibility,
            join_mode=join_mode or self.settings.default_join_mode,
            owner_cluster_id=owner_cluster_id,
            policy=policy or NetworkPolicy(),
            advertised_capabilities=list(advertised_capabilities or []),
            member_count=0,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )

        self._validate_network(network)

        await self.storage.put_network(network)
        return network

    async def get_network(
        self,
        network_id: str,
        *,
        require_active: bool = False,
    ) -> FederationNetwork | None:
        network = await self.storage.get_network(network_id)

        if network is None:
            return None

        if require_active and network.visibility == "private":
            return None

        return network

    async def require_network(self, network_id: str) -> FederationNetwork:
        network = await self.storage.get_network(network_id)

        if network is None:
            raise NetworkNotFoundError(f"FederationNetwork not found: {network_id}")

        return network

    async def get_network_by_slug(
        self,
        slug: str,
        *,
        include_private: bool = False,
    ) -> FederationNetwork | None:
        slug = validate_network_slug(slug)
        network = await self.storage.get_network_by_slug(slug)

        if network is None:
            return None

        if not include_private and network.visibility == "private":
            return None

        return network

    async def require_network_by_slug(
        self,
        slug: str,
        *,
        include_private: bool = False,
    ) -> FederationNetwork:
        network = await self.get_network_by_slug(
            slug,
            include_private=include_private,
        )

        if network is None:
            raise NetworkNotFoundError(f"FederationNetwork not found: {slug}")

        return network

    async def list_networks(
        self,
        *,
        include_private: bool = False,
        include_unlisted: bool = True,
    ) -> list[FederationNetwork]:
        networks = await self.storage.list_networks()

        visible: list[FederationNetwork] = []

        for network in networks:
            if network.visibility == "private" and not include_private:
                continue

            if network.visibility == "unlisted" and not include_unlisted:
                continue

            visible.append(network)

        visible.sort(key=lambda item: item.slug)
        return visible

    async def list_public_networks(self) -> list[FederationNetwork]:
        networks = await self.storage.list_networks()

        public_networks = [
            network for network in networks if network.visibility == "public"
        ]

        public_networks.sort(key=lambda item: item.slug)
        return public_networks

    async def update_network_metadata(
        self,
        network_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        visibility: FederationVisibility | None = None,
        join_mode: JoinMode | None = None,
        policy: NetworkPolicy | None = None,
        metadata: dict[str, object] | None = None,
    ) -> FederationNetwork:
        network = await self.require_network(network_id)

        if name is not None:
            network.name = name.strip()

        if description is not None:
            network.description = description

        if visibility is not None:
            network.visibility = visibility

        if join_mode is not None:
            network.join_mode = join_mode

        if policy is not None:
            network.policy = policy

        if metadata is not None:
            network.metadata.update(metadata)

        network.updated_at = utc_now()

        self._validate_network(network)

        await self.storage.put_network(network)
        return network

    async def update_advertised_capabilities(
        self,
        network_id: str,
        capabilities: Sequence[CapabilitySummary],
    ) -> FederationNetwork:
        network = await self.require_network(network_id)

        network.advertised_capabilities = list(capabilities)
        network.updated_at = utc_now()

        await self.storage.put_network(network)
        return network

    async def refresh_member_count(self, network_id: str) -> FederationNetwork:
        network = await self.require_network(network_id)
        members = await self.storage.list_members(network_id)

        network.member_count = len(
            [member for member in members if member.status == "active"]
        )
        network.updated_at = utc_now()

        await self.storage.put_network(network)
        return network

    async def delete_network(self, network_id: str) -> None:
        network = await self.storage.get_network(network_id)

        if network is None:
            raise NetworkNotFoundError(f"FederationNetwork not found: {network_id}")

        await self.storage.delete_network(network_id)

    def public_view(self, network: FederationNetwork) -> FederationNetwork:
        """
        Returns a public-safe network view.

        Network objects should already avoid internal service URLs and cluster
        internals. This method exists so API handlers have one clear place to
        apply public visibility rules later.
        """

        safe_network = network.model_copy(deep=True)

        if not safe_network.policy.expose_runtime_metadata:
            for capability in safe_network.advertised_capabilities:
                capability.metadata = {}

        return safe_network

    def _validate_network(self, network: FederationNetwork) -> None:
        validate_network_slug(network.slug)

        if not network.name.strip():
            raise NetworkRegistryError("FederationNetwork name cannot be empty")

        if not network.owner_cluster_id.strip():
            raise NetworkRegistryError(
                "FederationNetwork owner_cluster_id cannot be empty"
            )
