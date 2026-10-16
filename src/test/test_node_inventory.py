"""Run: PYTHONPATH=src python -m unittest discover -s src/test -p 'test_node_inventory.py'."""
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.protocol.routing_types import CanonicalTask, WorkPacket, WorkType
from memory import sql
from memory.runtime import MemoryRuntime


class NodeInventoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name in ("NOTES_DIR", "SUMMARIES_DIR", "DOCS_DIR", "YAML_DIR"):
            patcher = patch.object(sql, name, root / name.lower())
            patcher.start()
            self.addCleanup(patcher.stop)
        self.path = root / "memory.db"
        self.db = sql.MemoryDB(self.path)
        self.runtime = MemoryRuntime.__new__(MemoryRuntime)
        self.runtime._memoryDB = self.db
        self.node = {
            "alias": "orin-1",
            "machine_settings": {
                "host": "orin-node-1", "ip": "192.168.50.11", "platform": "jetson"
            },
            "ssh_config": {"default_user": "jetson"},
            "kubernetes_settings": {"k3s_node_name": "orin-1", "namespace": "orin"},
        }

    async def call(self, operation, **inputs):
        packet = WorkPacket(
            work_id="inventory-test",
            task=CanonicalTask(
                work_type=WorkType.MEMORY, operation=operation, inputs=inputs
            ),
        )
        # Expected validation failures are returned as failed WorkResults.
        with patch("memory.runtime.root_logger"):
            return await self.runtime.handle_ingress(packet)

    async def add(self):
        result = await self.call("inventory_add_node", namespace="orin", node=self.node)
        self.assertEqual(result.status, "completed", result.error)
        return result.metadata["record"]

    async def test_round_trip_survives_reopen(self):
        record = await self.add()
        reopened = sql.MemoryDB(self.path)
        self.assertEqual(reopened.get_inventory_node("orin", "orin-1"), record)
        self.assertEqual(record["revision"], 1)
        self.assertEqual(record["node"]["ssh_config"]["port"], 22)

    async def test_duplicate_add_does_not_overwrite(self):
        await self.add()
        self.node["machine_settings"]["host"] = "changed"
        result = await self.call("inventory_add_node", namespace="orin", node=self.node)
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.db.get_inventory_node("orin", "orin-1")["revision"], 1)
        self.assertEqual(
            self.db.get_inventory_node("orin", "orin-1")["node"]["machine_settings"]["host"],
            "orin-node-1",
        )

    async def test_update_and_stale_writer(self):
        original = await self.add()
        self.node["ssh_config"]["port"] = 2222
        result = await self.call(
            "inventory_update_node", namespace="orin", node=self.node, expected_revision=1
        )
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.metadata["record"]["revision"], 2)
        self.assertEqual(result.metadata["record"]["created_at"], original["created_at"])
        stale = await self.call(
            "inventory_update_node", namespace="orin", node=self.node, expected_revision=1
        )
        self.assertEqual(stale.status, "failed")
        self.assertEqual(self.db.get_inventory_node("orin", "orin-1")["revision"], 2)

    async def test_delete_checks_revision(self):
        await self.add()
        stale = await self.call(
            "inventory_delete_node", namespace="orin", alias="orin-1", expected_revision=2
        )
        self.assertEqual(stale.status, "failed")
        self.assertIsNotNone(self.db.get_inventory_node("orin", "orin-1"))
        deleted = await self.call(
            "inventory_delete_node", namespace="orin", alias="orin-1", expected_revision=1
        )
        self.assertEqual(deleted.status, "completed")
        self.assertTrue(deleted.metadata["deleted"])
        self.assertIsNone(sql.MemoryDB(self.path).get_inventory_node("orin", "orin-1"))

    async def test_namespace_isolation_and_missing_lookup(self):
        await self.add()
        result = await self.call("inventory_list_nodes", namespace="other")
        self.assertEqual(result.metadata["nodes"], [])
        result = await self.call("inventory_get_node", namespace="other", alias="orin-1")
        self.assertFalse(result.metadata["found"])
        self.assertIsNone(result.metadata["record"])
        result = await self.call("inventory_list_nodes", namespace="orin")
        self.assertEqual(result.metadata["count"], 1)

    async def test_invalid_records_do_not_write(self):
        invalid = []
        for field, value in (("ip", "not-an-ip"), ("platform", "unknown")):
            node = copy.deepcopy(self.node)
            node["machine_settings"][field] = value
            invalid.append(node)
        for field, value in (("port", 0), ("port", "22"), ("private_key", "secret"), ("password", "secret")):
            node = copy.deepcopy(self.node)
            node["ssh_config"][field] = value
            invalid.append(node)
        node = copy.deepcopy(self.node)
        node["kubernetes_settings"]["namespace"] = "other"
        invalid.append(node)
        for node in invalid:
            with self.subTest(node=node):
                result = await self.call("inventory_add_node", namespace="orin", node=node)
                self.assertEqual(result.status, "failed")
        self.assertEqual(self.db.list_inventory_nodes("orin"), [])

    async def test_database_failure_is_not_an_empty_inventory(self):
        with patch.object(self.db, "_connect", side_effect=sqlite3.OperationalError("unavailable")):
            result = await self.call("inventory_list_nodes", namespace="orin")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "unavailable")
        self.assertNotIn("nodes", result.metadata)

    async def test_additive_migration_preserves_existing_data(self):
        with self.db._connect() as conn:
            conn.execute("DROP TABLE node_inventory")
            conn.execute("CREATE TABLE upgrade_marker (value TEXT)")
            conn.execute("INSERT INTO upgrade_marker VALUES ('keep')")
        upgraded = sql.MemoryDB(self.path)
        self.assertEqual(upgraded.list_inventory_nodes("orin"), [])
        with upgraded._connect() as conn:
            self.assertEqual(conn.execute("SELECT value FROM upgrade_marker").fetchone()[0], "keep")

    async def test_required_revision_and_unknown_fields(self):
        await self.add()
        for operation, inputs in (
            ("inventory_update_node", {"node": self.node}),
            ("inventory_delete_node", {"alias": "orin-1"}),
            ("inventory_list_nodes", {"unexpected": True}),
        ):
            result = await self.call(operation, namespace="orin", **inputs)
            self.assertEqual(result.status, "failed")
        self.assertEqual(self.db.get_inventory_node("orin", "orin-1")["revision"], 1)


if __name__ == "__main__":
    unittest.main()
