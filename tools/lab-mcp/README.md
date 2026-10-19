# Orin Lab MCP

A small stdio MCP server for private lab diagnostics. It is independent of Cortex
and does not import Orin runtime code. Python 3.10 or newer is required. Install in
a separate virtual environment so MCP dependencies cannot change Cortex's runtime.

Tools:

| Tool | Purpose |
| --- | --- |
| `server_info` | Connector version, namespace and kubectl availability |
| `get_pods` | Pod readiness, restarts, nodes, image tags and image IDs |
| `pod_logs` | Current/previous container logs, 1–500 lines and at most 64 KiB |
| `get_nodes` | Actual Kubernetes Ready conditions and architecture |

Namespace defaults to `orin`. The operator may set `ORIN_LAB_NAMESPACE` before
starting the tunnel. The caller cannot choose a different namespace or execute a
shell command. `get_nodes` lists nodes cluster-wide. Every tool is marked read-only.
No secrets, exec, apply, delete, restart or arbitrary command tool is provided.

## Install on the gateway

Use a separate checkout; leave the checkout used for agent image builds alone:

```bash
git clone --branch lab-mcp-access --single-branch \
  https://github.com/ILGSupreme/Orin.git "$HOME/orin-lab-access"
cd "$HOME/orin-lab-access"
python3 -m venv .venv
.venv/bin/python -m pip install -r tools/lab-mcp/requirements.txt
kubectl -n orin get pods
```

Run as the ordinary gateway user that can already run kubectl, not with sudo.
The server uses that user's kubeconfig/current context. If needed, set
`KUBECONFIG` to its existing path and `ORIN_LAB_KUBECTL` to the absolute kubectl
executable path. The Kubernetes identity needs list/get pods, get pods/log, and
list nodes. The implementation limits available operations, but does not reduce
the underlying identity's privileges. Use a dedicated read-only Kubernetes
identity before expanding deployment or sharing access. Logs can contain private
application data and are returned to ChatGPT only when that tool is called.

## Replace the demo server in the existing tunnel

Stop the running demo client with Ctrl+C first. Run only one client for this
tunnel ID. Keep `CONTROL_PLANE_API_KEY` and `CONTROL_PLANE_TUNNEL_ID` exported in
the same terminal; the runtime key stays local and is not stored in this repo.

From the new checkout, create a separate profile:

```bash
tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile orin-lab \
  --tunnel-id "${CONTROL_PLANE_TUNNEL_ID:?Set your existing tunnel ID}" \
  --mcp-command "$PWD/.venv/bin/python $PWD/tools/lab-mcp/server.py"

tunnel-client doctor --profile orin-lab --explain
tunnel-client run --profile orin-lab --health.listen-addr 127.0.0.1:8787
```

This command assumes the checkout path contains no spaces. The client launches
the server as a child process over stdio; no MCP port or public server URL is
needed. Leave the foreground tunnel process running.

In another terminal:

```bash
curl -fsS http://127.0.0.1:8787/readyz
```

In ChatGPT, open the existing **Orin Lab** plugin and refresh its tools/metadata.
If refresh is unavailable, recreate the connection using the same tunnel. Keep
**No authentication** for this private initial connector; access depends on the
tunnel's organization/workspace associations and permissions. This is not a public
unauthenticated endpoint. Restart/reselect the conversation's plugin if its tools
remain stale. Expect `server_info`, `get_pods`, `pod_logs`, `get_nodes`, replacing
`echo` and `uppercase`.

Ask ChatGPT to call `server_info` and then `get_pods`. Readiness alone does not
prove lab access: setup is verified only when a real Kubernetes result comes back.

## Validation and limits

```bash
.venv/bin/python -m unittest discover -v -s tools/lab-mcp/tests
```

Tests use the real MCP client and server over stdio with a controlled kubectl
executable. They verify handshake/tool metadata, pod field filtering, previous
logs, argument rejection, node Ready conditions and permission error propagation.
They do not establish real kubeconfig access, RBAC or tunnel transport. Those are
checked on the gateway and through the installed ChatGPT plugin.

Kubernetes requests have a 10-second client deadline and 15-second subprocess
timeout. Logs are bounded; pod/node list sizes depend on the cluster. Local file
access by a user who controls the gateway remains outside the MCP tool boundary.
Persisting the process as a supervised service is a later step after live access
works. Build/deploy/test execution tools are not included in this first version.
