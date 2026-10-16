"""Cortex inventory tests against the real Memory runtime and temporary SQLite."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.protocol.routing_types import WorkPacket
from cortex.cluster.deployment.inventory import InventoryUnavailable, PersistentInventory
from cortex.cluster.deployment.types import KubernetesConfig, Machine, Node, SSHConfig
from memory import sql
from memory.runtime import MemoryRuntime


class Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class MemoryTransport:
    """Replace HTTP transport only; execute real Memory handlers and SQL."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.offline = False
        self.drop_write_reply = False
        self.invalid_list = False
        self.urls = []
        self.before_delete = None

    async def post(self, url, *, json, timeout):
        self.urls.append(url)
        if self.offline:
            raise OSError("Memory offline")
        packet = WorkPacket.model_validate(json)
        if self.before_delete and packet.operation == "inventory_delete_node":
            self.before_delete()
        with patch("memory.runtime.root_logger"):
            result = await self.runtime.handle_ingress(packet)
        if self.drop_write_reply and packet.operation == "inventory_add_node":
            raise OSError("Connection lost after commit")
        body = result.model_dump(mode="json")
        if self.invalid_list and packet.operation == "inventory_list_nodes":
            body["metadata"] = {}
        return Response(body)


class CortexInventoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "memory.db"
        for name in ("NOTES_DIR", "SUMMARIES_DIR", "DOCS_DIR", "YAML_DIR"):
            patcher = patch.object(sql, name, Path(temp.name) / name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runtime = MemoryRuntime.__new__(MemoryRuntime)
        self.runtime._memoryDB = sql.MemoryDB(self.path)
        self.transport = MemoryTransport(self.runtime)
        self.control = self.node("z-control", "192.0.2.1")
        self.worker = self.node("a-worker", "192.0.2.2")
        self.store = self.new_store()

    def node(self, alias, ip):
        return Node(
            alias, Machine(alias, ip, "jetson"), SSHConfig(default_user="jetson"),
            KubernetesConfig(k3s_node_name=alias, namespace="orin"),
        )

    def new_store(self):
        return PersistentInventory(
            http=self.transport, base_url="http://memory-authority:8080/",
            namespace="orin", control_node=self.control,
        )

    async def test_restart_loads_saved_worker_and_preserves_control_order(self):
        await self.store.add(self.worker)
        self.runtime._memoryDB = sql.MemoryDB(self.path)
        restarted = self.new_store()
        await restarted.refresh()
        self.assertEqual([n.alias for n in restarted.snapshot.nodes], ["z-control", "a-worker"])
        self.assertEqual(restarted.snapshot.getNode("a-worker").ssh_config.default_user, "jetson")
        self.assertTrue(all(url == "http://memory-authority:8080/work" for url in self.transport.urls))

    async def test_bootstrap_node_is_not_written_to_memory(self):
        await self.store.refresh()
        self.assertEqual(self.store.snapshot.nodes, [self.control])
        self.assertEqual(self.runtime._memoryDB.list_inventory_nodes("orin"), [])

    async def test_startup_failure_then_recovery(self):
        self.transport.offline = True
        with self.assertRaises(InventoryUnavailable):
            await self.store.refresh()
        with self.assertRaises(InventoryUnavailable):
            _ = self.store.snapshot
        self.transport.offline = False
        await self.store.refresh()
        self.assertIsNone(self.store.error)

    async def test_failed_refresh_does_not_expose_stale_inventory(self):
        await self.store.add(self.worker)
        self.transport.offline = True
        with self.assertRaises(InventoryUnavailable):
            await self.store.refresh()
        with self.assertRaises(InventoryUnavailable):
            _ = self.store.snapshot
        self.transport.offline = False
        await self.store.refresh()
        self.assertIsNotNone(self.store.snapshot.getNode("a-worker"))

    async def test_offline_write_never_reports_success(self):
        await self.store.refresh()
        self.transport.offline = True
        with self.assertRaises(InventoryUnavailable):
            await self.store.add(self.worker)
        self.assertEqual(self.runtime._memoryDB.list_inventory_nodes("orin"), [])

    async def test_lost_write_reply_recovers_from_database(self):
        self.transport.drop_write_reply = True
        with self.assertRaises(InventoryUnavailable):
            await self.store.add(self.worker)
        with self.assertRaises(InventoryUnavailable):
            _ = self.store.snapshot
        self.transport.drop_write_reply = False
        await self.store.refresh()
        self.assertIsNotNone(self.store.snapshot.getNode("a-worker"))
        with self.assertRaises(ValueError):
            await self.store.add(self.worker)

    async def test_delete_persists_and_control_is_protected(self):
        await self.store.add(self.worker)
        self.assertTrue(await self.store.remove("a-worker"))
        restarted = self.new_store()
        await restarted.refresh()
        self.assertIsNone(restarted.snapshot.getNode("a-worker"))
        self.assertFalse(await restarted.remove("missing"))
        with self.assertRaises(ValueError):
            await restarted.remove("z-control")

    async def test_revision_conflict_preserves_node(self):
        await self.store.add(self.worker)
        def concurrent_update():
            record = self.runtime._memoryDB.get_inventory_node("orin", "a-worker")
            node = copy.deepcopy(record["node"])
            node["ssh_config"]["port"] = 2222
            self.runtime._memoryDB.save_inventory_node("orin", node, record["revision"])
        self.transport.before_delete = concurrent_update
        with self.assertRaises(InventoryUnavailable):
            await self.store.remove("a-worker")
        self.assertIsNotNone(self.runtime._memoryDB.get_inventory_node("orin", "a-worker"))

    async def test_malformed_response_is_not_empty_inventory(self):
        self.transport.invalid_list = True
        with self.assertRaises(InventoryUnavailable):
            await self.store.refresh()
        with self.assertRaises(InventoryUnavailable):
            _ = self.store.snapshot


if __name__ == "__main__":
    unittest.main()
