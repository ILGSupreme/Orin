"""Persistent inventory backed by one configured Memory service."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING
from uuid import uuid4

from common.protocol.inventory_types import InventoryNode
from common.protocol.routing_types import CanonicalTask, WorkPacket, WorkResult, WorkType
from cortex.cluster.deployment.types import Inventory, KubernetesConfig, Machine, Node, SSHConfig

if TYPE_CHECKING:
    import httpx


class InventoryUnavailable(RuntimeError):
    pass


class PersistentInventory:
    def __init__(
        self, *, http: httpx.AsyncClient, base_url: str, namespace: str, control_node: Node
    ) -> None:
        self.http = http
        self.url = base_url.rstrip("/") + "/work"
        self.namespace = namespace
        self.control_node = control_node
        self._snapshot = Inventory()
        self._revisions: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self.error: str | None = "Inventory has not been loaded"

    @property
    def snapshot(self) -> Inventory:
        if self.error is not None:
            raise InventoryUnavailable(f"Node inventory unavailable: {self.error}")
        return self._snapshot

    async def _request(self, operation: str, **inputs) -> dict:
        packet = WorkPacket(
            work_id=f"inventory:{uuid4()}",
            task=CanonicalTask(
                work_type=WorkType.MEMORY, operation=operation,
                inputs={"namespace": self.namespace, **inputs},
            ),
        )
        try:
            response = await self.http.post(
                self.url, json=packet.model_dump(mode="json"), timeout=10.0
            )
            response.raise_for_status()
            result = WorkResult.model_validate(response.json())
            if result.work_id != packet.work_id:
                raise ValueError("Memory returned a different work_id")
            if result.status != "completed":
                raise ValueError(result.error or f"Memory returned {result.status}")
            return result.metadata
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            raise InventoryUnavailable(f"Node inventory unavailable: {self.error}") from exc

    def _decode_record(self, record: dict) -> tuple[Node, int]:
        validated = InventoryNode.model_validate(record["node"])
        data = validated.model_dump(mode="json")
        if record["namespace"] != self.namespace or record["alias"] != data["alias"]:
            raise ValueError("Invalid inventory record identity")
        revision = record["revision"]
        if type(revision) is not int or revision < 1:
            raise ValueError("Invalid inventory revision")
        settings = data["kubernetes_settings"]
        if settings is not None and settings["namespace"] != self.namespace:
            raise ValueError("Invalid node namespace")
        node = Node(
            alias=data["alias"],
            machine_settings=Machine(**data["machine_settings"]),
            ssh_config=SSHConfig(**data["ssh_config"]),
            kubernetes_settings=KubernetesConfig(**settings) if settings else None,
        )
        return node, revision

    async def _refresh(self) -> None:
        self.error = "Inventory refresh in progress"
        try:
            result = await self._request("inventory_list_nodes")
            records = result["nodes"]
            if not isinstance(records, list) or result.get("count") != len(records):
                raise ValueError("Invalid inventory list response")
            snapshot = Inventory(nodes=[self.control_node])
            revisions = {}
            for record in records:
                node, revision = self._decode_record(record)
                # Bootstrap configuration remains authoritative for the control host.
                if node.alias == self.control_node.alias:
                    raise ValueError("Persisted node conflicts with bootstrap control alias")
                snapshot.addNode(node)
                revisions[node.alias] = revision
            self._snapshot = snapshot
            self._revisions = revisions
            self.error = None
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            raise InventoryUnavailable(f"Node inventory unavailable: {self.error}") from exc

    async def refresh(self) -> None:
        async with self._lock:
            await self._refresh()

    async def add(self, node: Node) -> Node:
        # Validate before issuing any write.
        payload = InventoryNode.model_validate(asdict(node)).model_dump(mode="json")
        async with self._lock:
            await self._refresh()
            if self.snapshot.getNode(node.alias) is not None:
                raise ValueError(f"Node alias already exists: {node.alias}")
            result = await self._request("inventory_add_node", node=payload)
            try:
                saved, revision = self._decode_record(result["record"])
                if saved.alias != node.alias:
                    raise ValueError("Memory returned a different node alias")
            except Exception as exc:
                self.error = f"Invalid add response: {exc}"
                raise InventoryUnavailable(self.error) from exc
            self._snapshot.addNode(saved)
            self._revisions[saved.alias] = revision
            return saved

    async def remove(self, alias: str) -> bool:
        if alias == self.control_node.alias:
            raise ValueError("The bootstrap control node cannot be removed from inventory")
        async with self._lock:
            await self._refresh()
            if self.snapshot.getNode(alias) is None:
                return False
            result = await self._request(
                "inventory_delete_node", alias=alias,
                expected_revision=self._revisions[alias],
            )
            if result.get("deleted") is not True:
                self.error = "Memory did not confirm node deletion"
                raise InventoryUnavailable(self.error)
            self._snapshot.removeNode(alias)
            del self._revisions[alias]
            return True
