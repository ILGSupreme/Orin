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

This step adds storage and API operations only. Cortex does not yet load or save
inventory through these operations. Kubernetes nodes and pods are unaffected by
these database operations.

Use one authoritative Memory database for a cluster's inventory; do not route
inventory operations among independent Memory replicas/databases. Namespace
scoping is a storage partition, not authentication. These operations use the
existing internal Memory endpoint and must not be exposed as public/federated
configuration writes.

Deploy only after the storage-path checks in memory-storage-upgrade.md.
Cortex startup loading, unavailable-inventory behavior, and existing-node import
belong to the next step.

## Verification

~~~bash
PYTHONPATH=src python -m unittest discover -s src/test -p 'test_node_inventory.py'
~~~
