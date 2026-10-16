# Persistent node inventory: Memory API

Memory owns a dedicated SQLite node_inventory table. It is separate from chat
memories and federation records. The primary key is (namespace, alias).
Records include the validated node, a revision, and creation/update timestamps.
The table is created additively when Memory opens an existing database.

## WorkPacket operations

Submit to Memory's existing POST /work endpoint with task.work_type = "memory".
Inventory arguments go in task.inputs; no conversational memory_request or
user/session identity is required.

| Operation | Inputs | Successful result metadata |
| --- | --- | --- |
| inventory_list_nodes | namespace | nodes, count |
| inventory_get_node | namespace, alias | found, record (null if missing) |
| inventory_add_node | namespace, node | record (revision 1) |
| inventory_update_node | namespace, node, expected_revision | record (incremented revision) |
| inventory_delete_node | namespace, alias, expected_revision | deleted |

A record contains namespace, alias, node, revision, created_at, and updated_at.
Lists are complete and sorted by alias. This API targets small cluster inventories.

Example add request:

~~~json
{
  "work_id": "inventory-add-orin-1",
  "task": {
    "work_type": "memory",
    "operation": "inventory_add_node",
    "inputs": {
      "namespace": "orin",
      "node": {
        "alias": "orin-1",
        "machine_settings": {
          "host": "orin-node-1",
          "ip": "192.168.50.11",
          "platform": "jetson",
          "nvidia_gpu": true
        },
        "ssh_config": {
          "default_user": "jetson",
          "default_key": "/data/ssh/id_ed25519",
          "port": 22,
          "auth_method": "ssh_key"
        },
        "kubernetes_settings": {
          "k3s_node_name": "orin-1",
          "namespace": "orin"
        }
      }
    }
  }
}
~~~

## Validation and write semantics

- Shared schemas live in common/protocol/inventory_types.py. Unknown fields,
  invalid IPs/platforms/ports, and mismatched Kubernetes namespaces are rejected.
- SSH settings contain usernames and credential references, never private key
  contents, passwords, or k3s tokens. Those fields are not part of the schema.
- Add rejects duplicate aliases rather than replacing a record.
- Update is a complete replacement of node settings; alias is its identity.
  Update/delete require the current revision. A stale revision or missing record
  fails; clients must reload before retrying. Renaming is not an update operation.
- SQLite commits each mutation transactionally before success is returned.
- Validation, database errors, and conflicts return a failed WorkResult.
  HTTP success alone does not imply operation success.
- An unavailable database is an error, not an empty inventory.

## Integration boundary

Cortex loads persisted worker nodes at startup, after discovery is started.
The bootstrap control node comes from Cortex configuration and stays first in
the local snapshot. It is not written to Memory and cannot be removed through
/remove_node. A persisted record using its alias is rejected as a conflict.

Cortex uses the same WorkPacket API but pins requests to one authoritative URL.
Set inventory_memory_base_url in Cortex bootstrap configuration to override the
default http://memory-service.<namespace>.svc.cluster.local:8080. This is a base
URL; the client appends /work. Out-of-cluster development must set a reachable
URL. Requests have a 10-second timeout.

Node add/remove commands await durable confirmation before reporting success.
List, label, k3s install, and pod placement commands refresh the inventory first.
Writes are serialized within a Cortex process; revision checks protect deletes
against concurrent writers. Failed/ambiguous writes invalidate the local
snapshot. Reloading reconciles a write whose reply was lost.

If startup loading fails, Cortex stays available for terminal diagnostics.
Its /ready response includes inventory.ready and inventory.error alongside
the existing readiness fields; general readiness remains independent of Memory.
Inventory-dependent commands fail explicitly and retry loading when invoked
again. No background retry is required to recover. The status describes the
last attempted inventory operation, not a continuous health probe.

Kubernetes nodes and pods are unaffected by inventory add/remove operations.
There is no new terminal update-node command in this step.

Use one authoritative Memory database for a cluster's inventory; do not route
inventory operations among independent Memory replicas/databases. Namespace
scoping is a storage partition, not authentication. These operations use the
existing internal Memory endpoint and must not be exposed as public/federated
configuration writes.

## Upgrade order and existing nodes

1. Preserve existing Memory data as described in memory-storage-upgrade.md.
2. Before restarting the old Cortex process, record its /list_nodes output and
   the SSH usernames, ports, and key references used for each worker.
3. Deploy the updated Memory image and verify the inventory API is available.
4. Deploy the updated Cortex image.
5. Re-add previous worker entries with /add_node using those saved settings.
   This records inventory only; do not reinstall k3s just to restore inventory.
6. Restart Cortex and verify /list_nodes still contains the workers.

Old in-memory worker settings cannot be recovered automatically after the old
Cortex process exits. Kubernetes node objects do not contain all SSH settings,
so the client does not guess credentials or silently import incomplete records.

## Verification

~~~bash
PYTHONPATH=src python -m unittest discover -s src/test -p 'test_*inventory.py'
~~~
