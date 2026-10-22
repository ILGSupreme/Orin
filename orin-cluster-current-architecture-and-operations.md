# Orin: Current Architecture and Operations

Author: Samuel Padilla  
Documentation review: October 22, 2026  
Scope: source implementation and operating boundaries; deployed behavior requires
evidence for the exact revision, images and configuration.

This overview supersedes the older fixed chat pipeline and in-memory worker
inventory descriptions. Detailed source-era examples remain available in
[the prior revision](https://github.com/ILGSupreme/Orin/blob/4e90651cd37f5b21175a8e71342f946ba6626622/orin-cluster-current-architecture-and-operations.md);
they are historical, not current operating instructions.

## 1. Platform and responsibilities

Orin connects users with agents, generative models and executable capabilities
across local, edge and federated environments. Kubernetes/k3s is the current
deployment target, including Jetson and standard Linux hosts.

| Component | Responsibility |
| --- | --- |
| Cortex | Conversation, goals, execution, observations, feedback, discovery and routing; explicit local administration commands |
| Inference | Model execution through local Primer, separate cluster LLM services or configured provider routes |
| Tools | Defined executable operations within Cortex or separate authorized services |
| Memory | Conversation and durable operational state, goals, worker inventory and Federation records |
| Federation | Cross-environment membership, identity, policy and work/result exchange |
| Common | Shared protocols, adapters, model runtime and transient job infrastructure |

A node is a deployment host, not inherently an agent. Well-defined inference and
tool calls do not require independent agents. Automatic remote-agent delegation
is intended future work.

Discovery, authorization and execution remain distinct. Service metadata and
model output cannot grant permissions.

## 2. Source layout

```text
orin/
  canonical-unified-image/
  docs/
  src/
    common/
    cortex/
    federation/
    llm/
    memory/
    test/
  tools/
    lab-mcp/
    local-agent/
  install_orin.sh
  terminal-chat.py
```

There is no standalone `src/tool/` implementation. The Tool role is present in
routing and configuration, and built-in read-only inspection lives in Cortex.

## 3. Bootstrap and deployment

`install_orin.sh` bootstraps the initial k3s server, namespace, RBAC, SSH and
k3s-token secrets, configuration ConfigMap and persistent storage. It deploys
Memory, Cortex and Federation in that order and waits for their rollouts.

Relevant installer defaults include namespace `orin`, Cortex NodePort `30080`,
Federation NodePort `30081`, registry `orin-gw:5000`, image version `1.0.0`
and configuration path `/data/configuration`. These are source defaults, not
verified values for a running cluster. Review `bash install_orin.sh --help`
and the script before installation; bootstrap changes hosts and services.

The standard and Jetson Dockerfiles define separate native runtime requirements.
Source image names alone do not prove multi-architecture compatibility, GPU
availability or a working native model runtime. Validate each selected image
digest on the intended host.

Cortex deployment commands manage worker inventory, SSH-based k3s agent
installation, node labels, pod deployments and service discovery metadata.
Inventory changes are distinct from changes to Kubernetes nodes or workloads.

## 4. Natural conversation and persistent work

Both `POST /chat` and natural turns through `POST /terminal_chat` use the
same flow:

1. Harness resolves the user/session through Memory and persists input.
2. AgentRuntime interprets the turn using the configured inference gateway.
3. The turn can return an immediate reply and create durable tasks.
4. Ready tasks advance through bounded decisions, authorized actions and
   persisted observations. Dependencies restrict which tasks can proceed.
5. Completion cites successful observations from the current cycle or completed
   dependencies. A model assesses whether the evidence meets the outcome.
6. Results, questions and notices persist in Memory for polling and later turns.

The old `harness.chat` stage sequence and separate terminal/chat interpretation
schemas are superseded. Natural terminal requests can create persistent work.
Explicit terminal slash commands are dispatched directly before this flow.

Tasks advance round-robin within a goal rather than concurrently within that
goal. Clarification blocks dependent tasks while unrelated work can continue.
Feedback can reopen completed work and invalidate dependent results.

Memory owns durable goals, revisions and expiring execution claims. JobManager
owns transient jobs and generic pipeline utilities; it is not the source of
truth for responsibilities. A completed transient job is not proof that every
task in a goal succeeded.

Uncertain in-flight actions are paused for reconciliation after recovery.
Cancellation does not undo effects or guarantee that an already accepted backend
operation stops.

Explicit schedules and local events wake bounded runs. Arbitrary event
connectors, calendar rules and external notification delivery are not implemented
by the goal loop itself. See [the runtime](docs/cortex-agent.md).

## 5. Inference and executable capabilities

InferenceGateway has separate `interpret`, `decide` and `work` bindings.
Configured routes may use local Primer, discovered LLM services or an OpenAI
Responses provider. A separate LLM pod is not mandatory for Cortex-local inference.
Provider-only reasoning avoids eagerly initializing local native engines.

External inference is disabled by default. Operator enablement and an
external-allowed turn/goal privacy policy are both required. Model output cannot
authorize export. Goals retain their privacy policy and private context must not
leak through subsequent provider-enabled turns.

The built-in agent inventory includes analysis, summarization, input description,
local backend/job snapshots, node inventory and workloads. Local inspection uses
an explicit backend-free command context and an allowlist. It does not inherit
the user's selected command folder or arbitrary arguments.

Additional configured tools require a healthy discovered Tool backend with the
advertised capability and actual execution contract. Configuration does not
create a search service. Current-information requests require retrieval evidence.

Canonical messages can represent multiple modalities; actual support depends on
the ingress, adapter and model. The current provider adapter accepts text and image
input; arbitrary binary uploads, audio and video are not implemented there.
Image description requires a suitable work inference route.

## 6. Interfaces and readiness

| Service | Interface | Meaning |
| --- | --- | --- |
| Cortex | `POST /chat`, `POST /terminal_chat` | Natural turns; terminal ingress also supports explicit commands |
| Cortex | `GET /goals` | Scoped durable goal status, results and notices |
| Cortex | `POST /goals/{goal_id}/control` | Scoped resume, cancel and local event controls |
| Cortex | `POST /work`, `GET /work/{work_id}` | Direct LLM WorkPacket submission and transient polling |
| Cortex | `GET /network`, `GET /network/{role}` | Discovered local backends |
| Cortex | `GET /jobs/{job_id}` | Transient job details |
| Cortex | `/load`, `/unload`, `/engine`, `/model_status`, `/attach` | Local model administration and logs |
| LLM | `/work`, `/work/{work_id}`, `/model_status` | Inference work and runtime status |
| Memory | `POST /work` | Conversation and operational persistence operations |
| Runtime services | `/live`, `/ready`, `/health` | Service-specific diagnostics |

Cortex `/ready` reports inference route status, goal storage errors, Primer
status and inventory status. `accepting_requests` depends on inference readiness
and absence of an agent storage error. Inventory readiness is separately reported.
HTTP success or `ok: true` alone does not prove usable inference, healthy inventory
or successful end-to-end work.

Goal controls verify user/session scope. Caller-supplied identifiers and namespace
partitions are not a new authentication mechanism. Cortex and Memory interfaces
remain inside the trusted local administration boundary.

## 7. Protocol, discovery and routing

RuntimeMessages carry typed content parts. WorkPackets describe backend tasks,
routing hints and metadata; WorkResults distinguish accepted, running, completed
and failed work. Acceptance must be followed through retrieval to a terminal
result and assessed against the requested outcome.

RouterService submits and retrieves discovered backend work. BackendClient
handles local service transport. Federation uses its own client and sanitization
boundary rather than exposing internal BackendClient URLs.

Kubernetes discovery lists Services labelled `orin.ai/backend=true` in the
configured namespace, with role label `orin.ai/role`. Relevant annotations
include kind, model, capabilities, modalities, priority, weight and visibility.
EndpointSlices supply ready endpoint counts, including empty/omitted endpoint
cases. Runtime health and capability data inform backend selection.

Metadata is a routing claim, not authorization or evidence that a model can
complete a task. Exact selection and endpoint conventions are defined in the
discovery and routing modules.

## 8. Memory and node inventory

Memory stores conversation, claims, summaries and prompt context as well as
agent goals, worker inventory and Federation records. Goals use revision checks
and expiring claims. Worker inventory uses namespace/alias identity and
revision-checked writes against one authoritative Memory database.

The bootstrap control node comes from Cortex configuration. Worker records load
from Memory; local snapshots are refreshed for inventory-dependent commands.
Do not import SSH credentials by guessing from Kubernetes objects.
Inventory failures must be explicit rather than appearing as an empty cluster.

Before upgrading, inspect effective `MEMORY_BASE_DIR`, mounts and data location.
Older images may have stored data outside the intended PVC. Preserve a consistent
SQLite backup and associated files before replacing pods or changing mounts.
Local-path storage is not replicated storage or a node-loss backup.

Follow [storage upgrades](docs/memory-storage-upgrade.md) and
[node inventory](docs/node-inventory-api.md), including restoring old in-memory
worker settings before they are lost.

## 9. Commands and terminal client

CommandRouter uses structured commands, folders and `CommandContext`.
Handlers receive `(ctx, args)`. Backend-specific folders are derived from
discovered services; use `/commands` to inspect the actual current inventory.

Common navigation includes `/help`, `/commands`, `/cd`, `/render` and
`/attach`. Deployment, Cluster, Configuration and Federation provide scoped
commands. Administration remains an explicit user control; safe-command flags
do not expose every safe command to the autonomous agent.

`terminal-chat.py` defaults to `/terminal_chat`. Connection settings include
`ORIN_URL`, `ORIN_ENDPOINT`, `ORIN_USER_ID`, `ORIN_STREAM` and
`ORIN_CHAT_ONLY`. `ORIN_SESSION_ID` resumes a session; `ORIN_PRIVACY`
selects the turn's inference privacy policy. The client polls goals for results
and questions, deduplicates notices and shows the latest result on reconnect.

Streaming natural ingress currently returns the assembled reply rather than
incremental provider token streaming.

## 10. Federation implementation and limits

The HTTP MVP implements local network registry/listing, token redemption,
membership, Ed25519 identities, signed envelopes, replay/expiry checks, signed
heartbeats and capability publication. Memory stores network/member/token/work
records; identity keys and nonce/cache files remain local.

| Interface | Purpose |
| --- | --- |
| `GET /federation/networks` | List locally hosted public networks |
| `POST /federation/networks` | Create a local network; administrative |
| `GET /federation/networks/{slug}` | Public network view |
| `POST /federation/networks/{slug}/tokens` | Create a join token; administrative |
| `POST /federation/networks/{slug}/join` | Token-gated membership |
| `GET /federation/networks/{slug}/capabilities` | Public capability summary |
| `POST /federation/networks/{slug}/capabilities/refresh` | Refresh local advertisement; administrative |
| `POST /federation/networks/{network_id}/heartbeat` | Signed member heartbeat |
| `POST /federation/networks/{network_id}/capabilities` | Signed member capability publication |
| `POST /federation/networks/{network_id}/work` | Signed, policy-checked work submission |
| `GET /federation/networks/{network_id}/work/{work_id}` | Policy-enabled result polling |

Federation validates membership, signature, nonce/expiry and policy, sanitizes
the packet, submits through CortexBridge and persists a work record.
Cortex's direct ingress currently accepts LLM work only, even if a network policy
lists other work types. No agent-goal controls or local administration authority
transfer with membership.

Results clear internal backend identity and apply policy to exact models and
runtime metadata. Revalidate failure/error responses as well as successful
results before relying on public confidentiality.

Network/token administration lacks a complete authorization gate in the current
application. Result polling checks network policy but does not authenticate a
signed member on the GET path. Policy quota/concurrency fields do not prove
enforcement. The HTTP MVP is not a completed public deployment security model.

Local public listing is not decentralized global discovery. Automatic federated
backend selection, independent remote-agent delegation, volunteer bootstrap/
relay infrastructure and proposed peer protocols remain future work.
See the [Federation plan](docs/orin-federation-network-plan.md).

## 11. Read-only operating checks

Run cluster commands only against the intended kubeconfig and namespace:

```bash
kubectl get nodes -o wide
kubectl -n orin get pods,svc,pvc -o wide
kubectl -n orin logs deployment/cortex --tail=100
kubectl -n orin logs deployment/memory --tail=100
kubectl -n orin logs deployment/federation --tail=100
```

If using the installer's default host/ports, inspect:

```bash
curl -fsS http://orin-gw:30080/ready
curl -fsS http://orin-gw:30081/ready
ORIN_URL=http://orin-gw:30080 python terminal-chat.py
```

Substitute actual configured addresses. Inspect response fields and correlate logs;
a successful curl or Running pod is not proof of completed work. Do not expose
Cortex or Memory publicly as a workaround for missing Federation capabilities.

Before mutations, record configuration/data effects and obtain authorization
within scope. Preserve tested commits, image digests and relevant logs.

## 12. Validation and next experiments

Automated checks use real runtime/Memory logic with model, tool and provider HTTP
doubles, plus a local HTTP server for Kubernetes discovery. A separate CPU job
compiles GGUF grammars using the native parser without model weights.
See [CI](docs/ci.md); consult the exact commit's workflow for results.

The supplied June 5, 2026 checkpoint records a successful signed Federation
submit/poll flow with sanitized output. It is historical evidence and does not
validate the current code or deployed images.

The next useful acceptance experiment is a real read-only node/workload request:
prove the action executed, inspect its persisted observation and verify the goal's
answer against that evidence. Repeat representative requests with the actual
model, including denied/missing capabilities and recovery.

Then validate real retrieval/provider routes where configured, terminal behavior,
inventory persistence and current Federation submission/polling. Record remaining
failures rather than weakening authorization or evidence checks.

See [the runtime](docs/cortex-agent.md) and
[acceptance plan](docs/unified-agent-action-plan.md). Expand tools and remote
delegation only after the local execution and feedback loop is reliable.
