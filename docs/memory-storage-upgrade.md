# Memory storage upgrade

Memory deployments now explicitly set MEMORY_BASE_DIR to their PVC mount.
The installer uses /data/memory. Cortex-generated Memory deployments use
/data/memory by default, or the explicitly selected PVC mount path.
An explicit MEMORY_BASE_DIR must be inside that mount when a PVC is enabled.

## Existing deployments

Do not restart, replace, or rerun the installer against an existing Memory pod
until its data location has been checked and any necessary migration completed.
The old standard image defaults to /data/cluster-memory. The old Jetson image
does not set this variable itself; inspect its effective environment and working
directory rather than assuming where the database is stored.

1. Record the live pod's MEMORY_BASE_DIR, working directory, and volume mounts.
   Verify where memory.db and its notes, summaries, docs, and yaml directories
   actually reside.
2. Pause all writers to Memory, including Cortex and Federation operations.
   Keep the existing Memory pod alive while copying data.
3. Create a consistent SQLite backup using SQLite's backup API, and copy the
   associated file directories. Preserve an independent backup outside the pod.
   Do not rely on copying a live database file alone.
4. Copy the backed-up database and associated directories into the existing PVC,
   preserving relative paths. Inspect any files already there before copying;
   do not overwrite an existing database without reconciling the data.
5. Check the copied database with PRAGMA integrity_check and verify ownership
   and permissions for the Memory process.
6. Apply the updated deployment, confirm the effective MEMORY_BASE_DIR points
   inside the mounted PVC, and verify previously stored records are accessible.
   Then resume writers.
7. Verify data survives a controlled pod replacement before relying on the
   service for node inventory.

This source change does not migrate running pods or their existing data.
A local-path PVC survives pod replacement but does not provide replication
or protection against losing the storage node.
