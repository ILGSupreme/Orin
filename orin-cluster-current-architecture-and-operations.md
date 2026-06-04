# Orin Cluster - Current Architecture and Operations

Author: Samuel Padilla  
Scope: Current-state architecture and day-to-day operations for the Orin AI cluster  
Status: Cleaned current-state draft based on the current installer, Cortex/Harness, command router, JobManager, RouterService, discovery, Memory, LLM, Federation, and terminal client code

---

## 1. Purpose

Orin Cluster is a Kubernetes-based local edge-AI assistant platform. The active architecture is centered on **Cortex** as the local ingress, routing, command, and orchestration process. Runtime work is represented with canonical **WorkPackets** and routed to discovered backend services.

The current implementation is no longer best described as a gateway/orchestrator/Ollama split. The active shape is:

- **Cortex**: ingress, Harness, command interface, deployment operations, discovery, routing, local fast response, JobManager, and Memory/session integration.
- **LLM**: Primer-backed WorkPacket executor for model inference.
- **Memory**: WorkPacket backend for users, sessions, chat history, claims, notes, summaries, prompt context, and Federation records.
- **Tool**: planned backend role for non-LLM tools. The role is represented in protocols and deployment paths, but no complete tool backend is documented here yet.
- **Federation**: external communication boundary for public network discovery, token-gated joining, signed WorkPacket envelopes, capability advertisement, and federated work/result exchange.
- **Common**: shared protocol, runtime, Primer, adapter, JobManager, model-loading, and Hugging Face helpers.

The current direction is:

```text
WorkPacket is the common execution unit.
RouterService is the common routing/submission path.
JobManager is the common async job and pipeline mechanism.
Federation is the external membrane and must not expose internal cluster structure.
```

---

## 2. Repository structure

Current project shape:

```text
orin-cluster/
  canonical-unified-image/
  documentation/
  src/
    common/
    cortex/
    federation/
    llm/
    memory/
    tool/
  install-orin.sh
  terminal-chat.py
```

Important source areas:

```text
src/common/
  Shared protocols, RuntimeMessage, WorkPacket, WorkResult, Primer,
  adapters, JobManager, and runtime policies.

src/cortex/
  Cortex app, Harness, command router, deployment service,
  Kubernetes discovery, routing, planner, and backend client.

src/llm/
  Primer-backed LLM backend app.

src/memory/
  Memory backend app, SQLite/file-backed persistence, session/message logic,
  prompt context, and Federation storage operations.

src/federation/
  Federation models, settings, Memory-backed storage, network registry,
  join tokens, members, identity/signing, envelopes, policy,
  capability summaries, Cortex bridge, and outbound client.

src/tool/
  Planned backend role for non-LLM tools.
```

---

## 3. Image model

The intended image split is:

```text
standard runtime, amd64/arm64:
  FROM orin-gw:5000/primer-runtime:0.2

jetson runtime:
  FROM orin-gw:5000/primer-runtime:jetson-orin.0.2
```

The application folders can have standard and Jetson Dockerfiles:

```text
Dockerfile
Dockerfile.jetson
```

The long-term model should distinguish only between:

```text
standard runtime: amd64 and arm64
Jetson runtime: Jetson-specific base/runtime
```

Current cleanup note: some deployment code has carried three-way platform image selection for `jetson`, `arm64`, and `amd64`. The preferred direction is for `amd64` and `arm64` to share the standard runtime image naming while Jetson keeps its separate runtime image.

---

## 4. Bootstrap model: `install-orin.sh`

`install-orin.sh` is the initial host/bootstrap installer. It is not the long-term general pod deployment mechanism.

Its responsibilities are:

1. detect host platform and GPU state
2. optionally configure the local test registry for k3s/containerd
3. install k3s server on the bootstrap/control host
4. create namespace and RBAC
5. create the Cortex SSH-key secret
6. create the k3s join-token secret
7. write the deployment configuration file
8. create the deployment configuration ConfigMap
9. label the bootstrap node
10. deploy the initial Memory app
11. wait for Memory rollout
12. deploy the initial Cortex app
13. wait for Cortex rollout

### 4.1 Default bootstrap values

Important defaults:

```text
ORIN_NAMESPACE=orin
HOST_ALIAS=$(hostname)
HOST_NAME=$(hostname)
HOST_IP=$(hostname -I | awk '{print $1}')
K3S_NODE_NAME=$HOST_ALIAS
SSH_USER=ubuntu
SSH_KEY=~/.ssh/id_ed25519
SSH_KEY_MOUNT_PATH=/data/ssh/id_ed25519
SSH_KEY_SECRET_NAME=cortex-ssh-key
SSH_PORT=22
PLATFORM=auto
NVIDIA_GPU=auto
CORTEX_SIZE=light
CORTEX_NODE_PORT=30080
CORTEX_SERVICE_ACCOUNT=cortex
REGISTRY=orin-gw:5000
IMAGE_VERSION=1.0.0
CONFIG_PATH=/data/configuration
K3S_DATA_DIR=/data/k3s
K3S_STORAGE_DIR=/data/k3s-storage
CONFIGURE_LOCAL_REGISTRY=true
LOCAL_REGISTRY_ENDPOINT=http://${REGISTRY}
```

### 4.2 Configuration file

The installer writes JSON configuration to:

```text
/data/configuration
```

The Cortex pod receives that file through the ConfigMap:

```text
deployment-configuration
```

mounted at:

```text
/data/configuration
```

The configuration seeds the initial host/control node and deployment defaults. It is not the final source of truth for every future node. Additional nodes are added later through Cortex's command interface and `DeploymentService`.

### 4.3 Secrets

The installer creates:

```text
cortex-ssh-key
  mounted in Cortex at /data/ssh/id_ed25519

k3s-join-token
  contains key: token
```

Cortex uses the SSH key for node operations and reads the k3s join token through the Kubernetes API when installing k3s agents on additional nodes.

### 4.4 Bootstrap deployments

The installer deploys the minimum working runtime:

```text
memory
cortex
```

Cortex is exposed as:

```text
Service: cortex-service
Type: NodePort
NodePort: 30080
```

Memory is exposed internally as:

```text
Service: memory-service
Type: ClusterIP
```

Federation deployment support exists as a target direction, but this document treats the initial installer as a Cortex/Memory bootstrap path unless the installer has explicitly been extended for Federation in the current branch.

### 4.5 Bootstrap RBAC

The Cortex service account needs permissions to manage cluster resources and discover backends.

The installer creates a `ClusterRole` and `ClusterRoleBinding` for:

```text
nodes: get, list, watch, patch, update
namespaces: get, list, create
pods: get, list, watch, create, patch, update, delete
services: get, list, watch, create, patch, update, delete
persistentvolumeclaims: get, list, watch, create, patch, update, delete
configmaps: get, list, watch, create, patch, update, delete
secrets: get, list, watch
deployments: get, list, watch, create, patch, update, delete
endpointslices: get, list, watch
```

EndpointSlice access is required because backend discovery counts ready endpoints from EndpointSlices.

---

## 5. Runtime applications

### 5.1 Cortex

Cortex is the main local runtime. It owns:

- public ingress for terminal/chat requests
- Harness response flow
- slash-command handling
- local Primer-backed fast response path
- user/session/message persistence through Memory WorkPackets
- backend discovery and registry refresh
- backend routing and WorkPacket submission
- deployment operations through `DeploymentService`
- JobManager-based background jobs and pipelines
- direct WorkPacket handling for internal/Federation use

At startup, Cortex wires components such as:

```text
Primer
DeploymentService
DiscoveryService
BackendRoutingPolicy
BackendClient
Planner
RouterService
CommandRouter
JobManager
Harness
```

Cortex starts discovery refresh during lifespan startup and stops the Primer backend on shutdown.

### 5.2 Harness

Harness is the ingress layer for terminal and chat requests. It validates sessions, persists user messages through Memory, performs lightweight interpretation, optionally executes deterministic terminal actions, optionally starts background pipelines, and returns direct or fast responses.

Harness supports two ingress modes:

```text
terminal
chat
```

Terminal mode is command/help/status oriented and should not start background jobs.

Chat mode normally answers directly. It starts background work only when the lightweight interpreter chooses the `start_pipeline` action.

### 5.3 LLM

The LLM app is a Primer-backed WorkPacket executor. It exposes backend/model lifecycle endpoints and a packet execution API.

It supports:

```text
POST /engine or equivalent backend-engine loading path
POST /load
POST /unload
POST /work
GET  /work/{work_id}
GET  /model_status
GET  /live
GET  /ready
GET  /health
GET  /attach
```

The active discovery convention expects model/runtime status at:

```text
/model_status
```

This replaces older documentation that treated `/models` as the active discovery status endpoint.

Cortex discovers LLM services through Kubernetes service metadata and routes `work_type=llm` packets to them.

### 5.4 Memory

The Memory app is a synchronous WorkPacket backend. Cortex and Federation route Memory WorkPackets to it for:

- user upsert
- session resolution
- chat message creation
- recent conversation retrieval
- claims
- notes
- summaries
- prompt context
- Federation persistent records

The Memory backend normally returns completed `WorkResult` values directly from `POST /work`.

### 5.5 Tool

The Tool role is present in protocols, routing hints, deployment commands, and Federation public capability mapping. A complete current tool backend is not documented here yet.

### 5.6 Federation

Federation is the external communication boundary for Orin clusters.

It handles:

```text
public network discovery
token-gated network joining
member identity
signed member requests
signed federated work envelopes
policy enforcement
sanitized capability advertisement
federated work submission
federated result polling
```

Federation must not expose internal Orin cluster structure. External peers must not see Cortex URLs, LLM URLs, Memory URLs, Kubernetes services, pod names, node IPs, SSH details, deployment configuration, model paths, memory contents, slash commands, backend load/unload controls, or internal backend refs.

External communication goes through Federation endpoints only. Federation validates, verifies, authorizes, sanitizes, and then hands accepted work into local Cortex through `CortexBridge`.

---

## 6. Runtime endpoints

### 6.1 Cortex endpoints

| Endpoint | Method | Purpose |
|---|---:|---|
| `/work` | POST | Main ingress. In chat/terminal flows this is session-based; internal/Federation work can use WorkPacket-shaped requests where supported by the current Cortex app. |
| `/work/{work_id}` | GET | Poll internal/deferred work result where supported. |
| `/network` | GET | List discovered backend descriptors. |
| `/network/{role}` | GET | List discovered candidates for a role. |
| `/load` | POST | Start local Cortex model-load job when using Cortex-local Primer. |
| `/unload` | POST | Stop local Cortex Primer backend/model. |
| `/jobs/{job_id}` or command `/job_status` | GET/command | Inspect jobs depending on app surface. |
| `/attach` | GET | Stream local Cortex logs when configured. |
| `/live` | GET | Process liveness. |
| `/ready` | GET | Readiness and Primer status. |
| `/health` | GET | Basic health endpoint. |

### 6.2 LLM endpoints

| Endpoint | Method | Purpose |
|---|---:|---|
| `/engine` or backend-load equivalent | POST | Load/select `gguf` or `vllm` backend engine. |
| `/load` | POST | Start model-load job. |
| `/jobs/{job_id}` | GET | Read backend job status. |
| `/unload` | POST | Stop current backend/model. |
| `/work` | POST | Accept a `WorkPacket`. |
| `/work/{work_id}` | GET | Poll submitted LLM work. |
| `/model_status` | GET | Return loaded model/runtime metadata, including effective context. |
| `/attach` | GET | Stream backend logs. |
| `/live` | GET | Process liveness. |
| `/ready` | GET | Readiness and Primer status. |
| `/health` | GET | Basic health endpoint. |

### 6.3 Memory endpoints

| Endpoint | Method | Purpose |
|---|---:|---|
| `/work` | POST | Execute Memory `WorkPacket` operations synchronously. |
| `/work/{work_id}` | GET | Placeholder or lookup path; Memory work usually completes on POST. |
| `/live` | GET | Process liveness. |
| `/ready` | GET | Readiness. |
| `/health` | GET | Basic health endpoint. |

### 6.4 Federation endpoints

| Endpoint | Method | Purpose |
|---|---:|---|
| `/live` | GET | Process liveness. |
| `/ready` | GET | Readiness, cluster ID, protocol version. |
| `/health` | GET | Basic health and cluster ID. |
| `/federation/networks` | GET | List public Federation networks. |
| `/federation/networks` | POST | Create local Federation network. Local/admin endpoint. |
| `/federation/networks/{slug}` | GET | Get public-safe network view. |
| `/federation/networks/{slug}/tokens` | POST | Create join token. Local/admin endpoint. |
| `/federation/networks/{slug}/join` | POST | Join network using token, cluster ID, and public key. |
| `/federation/networks/{slug}/capabilities` | GET | Read public network capabilities. |
| `/federation/networks/{slug}/capabilities/refresh` | POST | Refresh local advertised capabilities from Cortex discovery. |
| `/federation/networks/{network_id}/heartbeat` | POST | Signed member heartbeat. |
| `/federation/networks/{network_id}/capabilities` | POST | Signed member capability publish. |
| `/federation/networks/{network_id}/work` | POST | Submit signed federated WorkPacket envelope. |
| `/federation/networks/{network_id}/work/{work_id}` | GET | Poll federated work result. |

---

## 7. Canonical runtime protocol

The current architecture uses internal canonical message and work types instead of treating OpenAI chat messages as the universal internal contract.

### 7.1 Runtime messages

```python
PartType = Literal["text", "image", "audio", "video", "json", "binary"]
PartEncoding = Literal["plain", "base64"]
Visibility = Literal["user", "internal"]

MessageRole = Literal[
    "system",
    "developer",
    "user",
    "assistant",
    "tool",
    "context",
]

class ContentPart(BaseModel):
    type: PartType
    data: str
    encoding: PartEncoding = "plain"
    mime_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class RuntimeMessage(BaseModel):
    role: MessageRole
    parts: list[ContentPart] = Field(default_factory=list)
    name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

Current implementation note: `ContentPart.mime_type` may be auto-filled from `type` and `encoding` when missing, for example `text/plain`.

When JSON-shaped payloads are embedded inside a text prompt for the model, prefer `type="text"`, `encoding="plain"`, and `mime_type="text/plain"` unless the part is intended to be treated as an actual `application/json` content part.

### 7.2 Ingress and egress

```python
class InferenceSession(BaseModel):
    user_id: str
    session_id: str | None = None
    channel: str = "api"
    stream: bool = False
    content: list[RuntimeMessage]
    metadata: dict[str, Any]

class EgressResponse(BaseModel):
    content: list[RuntimeMessage]
    work_id: str | None = None
    session_id: str | None = None
    metadata: dict[str, object] = {}
```

### 7.3 Work packets

```python
class WorkType(str, Enum):
    LLM = "llm"
    TOOL = "tool"
    CORTEX = "cortex"
    MEMORY = "memory"

class WorkDisposition(str, Enum):
    DIRECT = "direct"
    DEFERRED = "deferred"

class RoutingHints(BaseModel):
    role: WorkType | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    required_modalities: list[str] = Field(default_factory=list)
    runtime_preference: list[dict[str, Any]] = Field(default_factory=list)
    latency_preference: str = "normal"

class CanonicalTask(BaseModel):
    work_type: WorkType
    operation: str
    messages: list[RuntimeMessage] = Field(default_factory=list)
    memory_request: BaseRuntimeMemoryRequest | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    routing_hints: RoutingHints = Field(default_factory=RoutingHints)

class WorkPacket(BaseModel):
    work_id: str
    disposition: WorkDisposition = WorkDisposition.DIRECT
    task: CanonicalTask
    metadata: dict[str, Any] = Field(default_factory=dict)

class WorkResult(BaseModel):
    status: str
    work_id: str
    content: list[RuntimeMessage] = Field(default_factory=list)
    backend_name: str | None = None
    backend_model: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

Allowed `WorkResult.status` values:

```text
accepted
running
completed
failed
```

Important current field name:

```text
memory_request
```

Older text or code that says `memoryrequest` is stale.

### 7.4 WorkResult federation sanitization

`WorkResult.sanitize_for_federation()` returns a reduced WorkResult intended for external Federation egress.

It preserves:

```text
status
work_id
content
backend_name
backend_model
error
```

It drops most metadata and only keeps sanitized `work_details` when present:

```text
work_type
operation
```

This prevents internal routing metadata such as backend refs, selected backend details, trusted internal flags, or local execution context from leaking through Federation.

---

## 8. Harness ingress and response flow

Harness validates the incoming session, persists the user message through Memory, performs lightweight interpretation, optionally executes a deterministic terminal action or starts a background pipeline, and then returns either a direct response or a fast model-generated response.

### 8.1 Session validation

For each ingress request, Harness:

1. validates the incoming `InferenceSession` as an `InferenceObject`
2. upserts the user through Memory
3. resolves or creates a session through Memory if no `session_id` was provided
4. inserts the incoming user message into Memory
5. continues to terminal or chat response handling

### 8.2 Lightweight interpretation

Harness builds a small interpretation context containing:

```text
Ingress context:
- mode
- user_id
- session_id
- channel

Session background jobs:
- compact job summary, when jobs exist

Available mode commands/actions:
- command_router.get_commands(mode)
```

This context is intentionally small. It gives the lightweight interpreter enough information to choose an action without exposing full backend state or large runtime details.

### 8.3 Terminal mode

Terminal mode is for CLI help, deterministic inspection, and safe terminal/system information actions. It should not start background work.

Terminal interpretation model:

```json
{
  "mode": "terminal",
  "intent": "terminal_help | system_status | job_status | normal_chat | unknown",
  "action": "answer_only | terminal_info_action | report_job_status",
  "terminal_action": "string | null"
}
```

If the action is `terminal_info_action`, Harness executes the selected command-router action through:

```python
command_router.execute_harness_action(
    terminal_action,
    mode="terminal",
)
```

Only safe information actions are executed automatically.

If the action is `report_job_status`, Harness reads session jobs from JobManager and formats session job status.

### 8.4 Chat mode

Chat mode is the normal assistant path.

Chat interpretation model:

```json
{
  "mode": "chat",
  "action": "answer_only | start_pipeline | report_job_status",
  "pipeline": "chat | null"
}
```

Normal questions should use `answer_only`.

Background work starts when the user explicitly asks for deeper, detailed, researched, verified, current, multi-step, or comprehensive work. When the action is `start_pipeline`, Harness starts a JobManager pipeline:

```text
kind: harness.chat
pipeline: chat
```

The immediate response does not wait for the full result:

```text
A deeper response is being worked on. Background job `<job_id>` has been started. Ask for the status or result later.
```

If the action is `report_job_status`, Harness returns formatted job status for the current session.

### 8.5 Fast response path

If Harness does not return a direct terminal/action/job-status response, it builds a fast response prompt.

The fast response prompt can include:

- current user message
- last assistant message from Memory
- compact response context
- internal response instruction derived from lightweight interpretation
- background job id, if one was started

The fast response is generated through the local Primer using the `chat` generation policy.

### 8.6 Chat background pipeline

The current `chat` pipeline stages are:

```text
get_prompt
interpret_turn
shape_turn
build_packets
read_and_send_packets
build_final_response_message
```

Stage responsibilities:

```text
get_prompt:
  Fetch prompt context from Memory for the current session.

interpret_turn:
  Build turn-interpretation messages and route an llm.inspect WorkPacket
  through RouterService. Primer is used for token estimation; actual work
  is routed to an LLM backend.

shape_turn:
  Read the interpretation result, build task-shaping messages, and route
  another llm.inspect WorkPacket through RouterService.

build_packets:
  Convert shaped tasks into canonical WorkPackets.

read_and_send_packets:
  Submit built WorkPackets through router.send_many(), wait for send batch,
  call router.retrieve_many(), and wait for retrieve batch.

build_final_response_message:
  Build the final response prompt from the original user message, prompt
  context, and collated packet results. Then route an llm.analyze WorkPacket
  and poll until completion or failure.
```

The shaper output is normalized before packet creation. Accepted shapes include:

```text
task_items
items
tasks
```

The normalization step guards against the model incorrectly routing LLM-style work to Cortex. Operations such as `chat`, `summarize`, `classify`, `extract`, and `analyze` are normalized toward `llm` routing when appropriate.

### 8.7 WorkPacket ingress pipeline

Harness also defines a smaller `work_pipeline` for direct WorkPacket ingress.

Current behavior:

1. If `packet.task.work_type == "llm"`, start pipeline `work_pipeline`.
2. Return `WorkResult(status="accepted", work_id=<job_id>)`.
3. Store the original packet work id in metadata as `original_work_id`.
4. Poll the JobManager job through `retrieve_work_packet()`.
5. Return a sanitized WorkResult for federation-safe egress.

The current direct WorkPacket path only accepts LLM work. Other work types return failed with `unknown work type`.

### 8.8 Current implementation note

The Harness implementation contains branches for a `get_job_result` action, but the current lightweight interpretation model does not include `get_job_result` as a valid action. Treat this as an implementation stub until the type model is updated.

---

## 9. Command interface

The command router exposes a folder-style terminal interface. Commands and folders are represented as structured objects, and the same command inventory is also exposed to Harness for lightweight interpretation.

### 9.1 Command model

Main concepts:

```text
ShellState
  path: list[str]
  selected_backend_name: str | None

CommandContext
  router: CommandRouter
  backend: BackendDescriptor | None
  backend_client -> router.backend_client

Command
  command: str
  desc: str
  kind: "text" | "stream"
  handler
  modes: set["terminal" | "chat"]
  harness_action: str | None
  safe_info_action: bool
  suggest_only: bool

CommandFolder
  name: str
  desc: str
  commands: list[Command]
  folders: dict[str, CommandFolder]
  dynamic: bool
```

All text command handlers use:

```python
handler(ctx: CommandContext, args: str)
```

Stream commands use the same context but return an async iterator.

`CommandContext` is important because backend-scoped commands no longer need special hardcoded router paths. If the shell is currently inside `Root / Cluster / <backend>`, the context contains the selected backend. Otherwise `ctx.backend` is `None`.

### 9.2 Global commands

Global commands:

```text
/help
/commands
/cd
/render
/attach
```

`/attach` is a stream command. In a backend folder it attaches to the selected backend's `/attach` endpoint. Outside a backend folder it attaches to the local Cortex log stream, if configured.

### 9.3 Current folder tree

```text
Root
  /help
  /commands
  /cd
  /attach
  /render

  Deployment
    /list_pods
    /list_nodes
    /deploy_pod
    /delete_pod
    /install_k3s
    /add_node
    /remove_node
    /label_node
    /update_discovery_meta

  Cluster
    /show
    /cluster_snapshot
    <dynamic backend folders>

  Configuration
    /model_aliases
    /job_status
    /load_model
    /unload_model
    /engine

    ModelDownloader
      /huggingface
```

The old backend `/network` command has been replaced by `/show` in the Cluster context.

### 9.4 Dynamic Cluster folder

`Cluster` is a dynamic folder. When the user enters:

```text
/cd Cluster
```

the router lists discovered backends from the backend registry and exposes each backend as a child folder.

Example:

```text
Root / Cluster
  /show
  /cluster_snapshot

  llm-medium-1-service
  memory-service
```

When the user enters a backend folder:

```text
/cd llm-medium-1-service
```

the router sets:

```python
shell_state.path = ["Root", "Cluster", backend.name]
shell_state.selected_backend_name = backend.name
```

The current folder is then resolved from the selected backend role. If the backend role is known, the role-specific folder is used. Otherwise the generic backend folder is used.

### 9.5 Backend command folders

Generic backend commands:

```text
/show
/health
/update_discovery_meta
```

LLM backend commands:

```text
/show
/health
/update_discovery_meta
/model_status
/load_model
/job_status
/unload_model
```

Memory currently uses the generic backend command folder.

Future role folders can be added for Tool, Cortex, and Federation.

### 9.6 Human visibility versus Harness action inventory

The human terminal remains folder-oriented. `/commands` shows the commands visible in the current folder plus global commands.

Harness is different. The AI should not be limited by the human user's current CLI folder. `CommandRouter.get_commands(mode)` returns the broader action inventory available to the lightweight interpreter.

Each returned command entry contains:

```text
action
command
description
folder
safe_info_action
suggest_only
requires_backend
backend_role
```

This lets Harness decide whether a user request can be fulfilled by a safe deterministic terminal action.

### 9.7 Safe and suggest-only actions

Commands are tagged for Harness use.

```text
safe_info_action=True
```

means Harness may execute the command automatically for information retrieval.

```text
suggest_only=True
```

means Harness should not execute the command automatically. It can suggest the command or explain it, but execution requires the user to explicitly issue that command.

Examples of safe information actions:

```text
list_commands
render_current_folder
list_pods
list_nodes
show_cluster
cluster_snapshot
model_status
job_status
show_backend
backend_health
```

Examples of suggest-only or mutating actions:

```text
change_folder
attach_logs
deploy_pod
delete_pod
install_k3s
add_node
remove_node
label_node
update_discovery_meta
load_model
unload_model
engine_status
huggingface
```

`execute_harness_action()` enforces this at runtime. It rejects unknown actions, non-safe actions, and suggest-only actions.

### 9.8 Model and Hugging Face commands

Current model alias examples:

```text
qwen35-0.8b
qwen35-2B
qwen35-4b
qwen35-9b
```

The `/huggingface` command supports:

```text
/huggingface search <query>
/huggingface info <repo_id>
/huggingface gguf <repo_id>
/huggingface alias add <alias> <repo_id> [--file filename] [--tokenizer tokenizer_id]
```

---

## 10. JobManager

`JobManager` is the shared runtime mechanism for asynchronous jobs, background pipelines, command-triggered long-running work, router batch work, and Harness background work.

It is generic and is not model-load specific.

### 10.1 Core job types

```python
JobStatus = Literal["accepted", "running", "completed", "failed"]
StageStatus = Literal["accepted", "running", "completed", "failed"]

@dataclass(slots=True)
class JobSpec:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    job_id: str | None = None

@dataclass(slots=True)
class Job:
    job_id: str
    spec: JobSpec
    status: JobStatus = "accepted"
    error: str | None = None

    pipeline_name: str | None = None
    current_stage: str | None = None
    stage_index: int = 0
    stage_count: int = 0
    stage_status: StageStatus | None = None
    stage_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    latest_result: dict[str, Any] = field(default_factory=dict)
    progress_message: str | None = None
```

A job can be either a single runner job or a pipeline job with multiple stages.

Single jobs are used for simple async operations such as router send/retrieve jobs and local model-load jobs.

Pipeline jobs are used for staged workflows such as Harness background chat.

### 10.2 Pipeline model

A pipeline is a named list of stages:

```python
@dataclass(slots=True)
class PipelineStage:
    name: str
    runner: PipelineStageRunner
    poller: PipelineStagePoller | None = None
    timeout_seconds: float = 120.0
    poll_interval_seconds: float = 1.0

@dataclass(slots=True)
class PipelineDefinition:
    name: str
    stages: list[PipelineStage]
```

If the runner returns `completed` or `failed`, the stage ends immediately.

If the runner returns `accepted` or `running`, the stage must have a poller. The poller is called until the result becomes `completed` or `failed`, or until the stage timeout expires.

If a stage returns `accepted` or `running` without a poller, the stage fails.

### 10.3 Separate job and pipeline semaphores

`JobManager` uses two separate semaphores:

```text
job_semaphore
pipeline_semaphore
```

This avoids a deadlock where a parent pipeline holds the only job slot while waiting for child router jobs that also need the same slot.

Normal jobs run under `job_semaphore`.

Pipeline jobs run under `pipeline_semaphore`.

This means a running pipeline can safely start child jobs through RouterService and wait for their batch results.

### 10.4 Starting jobs and pipelines

Normal job:

```python
job_manager.start(
    spec=JobSpec(kind="...", payload={...}),
    runner=runner,
    on_completed=optional_hook,
)
```

Pipeline job:

```python
job_manager.define_pipeline("chat", stages=[...])

job_manager.start_pipeline(
    spec=JobSpec(kind="harness.chat", payload={...}),
    pipeline="chat",
)
```

The returned job starts as `accepted`. When execution begins, it becomes `running`.

When a runner returns, the result is converted to a JSON-safe dictionary with:

```python
formatting.as_dict(result)
```

and stored in:

```text
job.latest_result
```

If the runner returns a `WorkResult` with `status="failed"`, the job is marked failed and `job.error` is set from the WorkResult error.

### 10.5 Batch work

`JobManager` supports lightweight batches through `BatchWork`:

```python
@dataclass(slots=True)
class BatchWork:
    job_ids: list[str] = field(default_factory=list)
    batch_id: str = ""
```

A batch stores only job IDs. It does not store `asyncio.Task` objects.

Creating a batch:

```python
batch_id = job_manager.create_batch(jobs)
```

Waiting for a batch:

```python
await job_manager.batch_progress(batch_id)
```

`batch_progress()` returns a list of normalized job results once every job in the batch has status `completed` or `failed`.

Default behavior:

```text
blocking: true
poll interval: 1 second
timeout: 300 seconds
```

### 10.6 Job inspection

Useful inspection methods:

```text
get(job_id)
get_progress(job_id)
list_jobs()
list_active_jobs()
list_jobs_for_session(session_id)
list_active_jobs_for_session(session_id)
summarize_jobs_for_prompt(session_id)
```

Current status meanings:

```text
accepted  = job has been created and queued
running   = job or pipeline stage is currently running
completed = job finished successfully
failed    = job failed
```

Current implementation note: `summarize_jobs_for_prompt()` includes payload details for completed jobs. This is useful while debugging but may be too noisy for model-facing summaries. Long term, completed job summaries should prefer a compact result summary.

---

## 11. Deployment service and inventory

`DeploymentService` owns the mutable in-memory `Inventory`. It reads `/data/configuration` during startup and seeds the initial host/control node into inventory.

### 11.1 Inventory model

```text
Inventory
  nodes: list[Node]

Node
  alias
  machine_settings
    host
    ip
    platform: amd64 | arm64 | jetson
    nvidia_gpu
  ssh_config
    default_user
    default_key
    port
    auth_method
  kubernetes_settings
    k3s_node_name
    namespace
    data_dir=/data/k3s
    default_local_storage_path=/data/k3s-storage
```

### 11.2 DeploymentService responsibilities

`DeploymentService` supports:

- adding/removing inventory nodes
- listing inventory nodes with Kubernetes status
- labeling Kubernetes nodes
- checking passwordless sudo over SSH
- installing k3s server/agent over SSH
- deploying pod apps through the Kubernetes Python client
- deleting pod apps
- listing deployed pod apps
- patching service discovery metadata
- resolving pod placement

### 11.3 Node placement labels

Node labels are used for scheduling and compatibility:

```text
orin.role.backend=true
orin.platform=<amd64|arm64|jetson>
orin.gpu=<true|false>
```

Optional role preference labels are soft scheduling hints:

```text
orin.role.cortex=true
orin.role.llm=true
orin.role.tool=true
orin.role.memory=true
```

These are not the same as service discovery labels.

### 11.4 Service discovery metadata

Service labels and annotations are used for backend discovery and routing.

Required service labels:

```yaml
labels:
  orin.ai/backend: "true"
  orin.ai/role: "<role>"
```

Supported discovery annotations:

```yaml
annotations:
  orin.ai/kind: "<kind>"
  orin.ai/model: "<model-id>"
  orin.ai/capabilities: "[\"chat\"]"
  orin.ai/modalities: "[\"text\"]"
  orin.ai/priority: "100"
  orin.ai/weight: "1.0"
  orin.ai/visibility: "internal"
```

Current convention-based paths:

```text
health_path = /health
work_path = /work
model_status_path = /model_status for llm and cortex
model_status_path = None for other roles
```

Older annotations such as these are no longer required by the current discovery provider:

```text
orin.ai/health_path
orin.ai/work_path
orin.ai/models_path
```

The current discovery provider hardcodes the standard endpoint convention instead.

---

## 12. Pod application model

The deployment code uses generic app specs:

```text
PodDeploymentSpec
PodServiceSpec
PodPvcSpec
PodDiscoverySpec
PodProbeSpec
PlacementDecision
```

For an app named `<app_name>`, generated resources are:

```text
Deployment: <app_name>
Service:    <app_name>-service
PVC:        <app_name>-pvc
```

Cortex is deployed as NodePort. LLM, Memory, Tool, and Federation app services should generally be ClusterIP unless explicitly exposed.

Current probe convention:

```text
readiness: /ready
liveness:  /live
startup:   /ready
```

Current storage profiles:

```text
light  -> 20Gi
medium -> 50Gi
high   -> 100Gi
```

Current cleanup note: some command/type definitions may still use `strong` instead of `high`.

---

## 13. Discovery, health, and routing

Cortex discovers routable backends from Kubernetes Services. Discovery is service-based, not pod-based.

A backend is any Kubernetes Service in the configured Cortex namespace with:

```text
orin.ai/backend=true
```

The service must also provide a role label:

```text
orin.ai/role=<role>
```

Known backend roles:

```text
cortex
llm
tool
memory
```

### 13.1 Discovery service composition

`DiscoveryService` wires together:

```text
KubernetesDiscoveryProvider
BackendHealthProber
InMemoryBackendRegistry
RegistryRefresher
BackendSelector
```

It is initialized with defaults such as:

```text
refresh_interval_seconds = 15.0
health_timeout = 2.0
```

The registry is refreshed periodically by `RegistryRefresher`.

### 13.2 Kubernetes service discovery

`KubernetesDiscoveryProvider` lists Services in the configured Cortex namespace using:

```text
label_selector = "orin.ai/backend=true"
```

It also lists EndpointSlices in the same namespace and counts ready endpoints per service.

A service is skipped if:

```text
orin.ai/role is missing
no usable service port exists
```

The service port selection prefers a port named `http`; otherwise discovery uses the first service port.

### 13.3 Backend URL convention

Backend URLs are built from internal Kubernetes DNS:

```text
http://<service-name>.<namespace>.svc.cluster.local:<port>
```

Example:

```text
http://llm-medium-1-service.orin.svc.cluster.local:8080
```

This internal service URL is used only inside the Orin cluster. Federation must not expose it externally.

### 13.4 BackendDescriptor

Discovered services become `BackendDescriptor` objects:

```python
@dataclasses.dataclass(slots=True)
class BackendDescriptor:
    name: str
    namespace: str
    service_name: str
    url: str

    role: BackendRole | str
    kind: str | None = None
    model: str | None = None

    capabilities: list[str] = field(default_factory=list)
    modalities: list[str] = field(default_factory=list)

    priority: int = 100
    weight: float = 1.0
    visibility: str = "internal"

    health_path: str = "/health"
    work_path: str = "/"
    model_status_path: str | None = None

    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)

    health: BackendHealth = field(default_factory=BackendHealth)
    runtime: RuntimeMetaData = field(default_factory=RuntimeMetaData)
```

The descriptor name is currently:

```text
<namespace>/<service-name>
```

Example:

```text
orin/llm-medium-1-service
```

### 13.5 Health probing

`BackendHealthProber` probes every discovered backend.

Probe flow:

1. Check whether the service has at least one ready EndpointSlice endpoint.
2. If no ready endpoints exist, mark backend unavailable.
3. If ready endpoints exist, call `<backend.url>/health`.
4. If `/health` fails, mark backend degraded.
5. If the backend has `model_status_path`, call `<backend.url>/model_status`.
6. If model status fails, mark backend degraded.
7. If all required checks pass, mark backend healthy.

For `llm` and `cortex` backends, discovery attempts model status probing through:

```text
/model_status
```

If the response contains runtime metadata, the prober updates:

```text
backend.runtime.effective_n_ctx
```

This value is later used for runtime-aware backend selection.

### 13.6 Runtime metadata

Current runtime metadata:

```python
@dataclasses.dataclass(slots=True)
class RuntimeMetaData:
    effective_n_ctx: int = 0
```

Example runtime preference:

```text
runtime_preference = [{"effective_n_ctx": 6000}]
```

This means the backend must have `effective_n_ctx >= 6000`.

### 13.7 Capability summary

`DiscoveryService.get_capability_summary()` returns a compact summary for task shaping and lightweight orchestration.

Shape:

```json
{
  "roles": ["llm", "memory"],
  "capabilities_by_role": {
    "llm": ["chat", "summarize", "analyze"]
  },
  "modalities_by_role": {
    "llm": ["text"]
  },
  "deferred_available": false
}
```

`deferred_available` is currently true when a discovered backend has role `cortex`.

### 13.8 Backend selection

Backend selection is handled by `BackendSelector`.

A selection request can specify:

```python
@dataclass(slots=True)
class BackendSelectionRequest:
    role: str | None = None
    required_capabilities: list[str] | None = None
    required_modalities: list[str] | None = None
    runtime_preference: list[dict[str, Any]] | None = None
    healthy_only: bool = True
```

Selection filters backends in this order:

1. `healthy_only`
2. `runtime_preference`
3. `role`
4. `required_capabilities`
5. `required_modalities`

After filtering, candidates are sorted by:

```text
healthy first
priority descending
weight descending
name ascending
```

### 13.9 Backend routing policy and Planner

`BackendRoutingPolicy` performs primary healthy selection first. If no healthy backend matches and degraded fallback is allowed, it retries with `healthy_only=False` and may return a degraded backend.

`Planner` is intentionally thin. It converts WorkPacket routing requirements into a backend selection request:

```python
class Planner:
    def select_backend(self, packet: WorkPacket):
        return self.routing_policy.select_backend(
            role=packet.required_role,
            required_capabilities=packet.required_capabilities,
            required_modalities=packet.required_modalities,
            runtime_preference=packet.routing_hints.runtime_preference,
        )
```

---

## 14. RouterService and BackendClient

### 14.1 RouterService

`RouterService` is the central dispatch layer for WorkPackets.

It owns:

```text
backend selection
packet submission
accepted-work metadata
polling
batch send/retrieve operations
```

Dependencies:

```text
DiscoveryService
BackendClient
JobManager
Planner
```

### 14.2 Sending one WorkPacket

Main method:

```python
await router.send(packet)
```

Send flow:

1. Resolve backend.
2. Submit packet through `BackendClient`.
3. If backend returns `completed` or `failed`, return that WorkResult.
4. If backend returns `accepted`, attach backend metadata and return accepted WorkResult.
5. If response shape is invalid, return failed WorkResult.

Backend resolution first checks:

```python
packet.metadata["backend_name"]
```

If no explicit backend is found, RouterService calls:

```python
planner.select_backend(packet)
```

### 14.3 Accepted WorkResult contract

When a backend accepts work asynchronously, RouterService returns a `WorkResult` with:

```text
status = accepted
work_id = backend work id
backend_name = selected backend name
backend_model = selected backend model
metadata.backend_ref = serialized backend reference
metadata.work_details = work type and operation
```

`backend_ref` contains only the information needed to retrieve the result later:

```text
name
namespace
service_name
url
work_path
```

`work_details` contains:

```text
work_type
operation
```

This is the contract for accepted or running work. Retrieval code should use `metadata["backend_ref"]`, not the older `backend_desc`.

### 14.4 Retrieval and polling

Single retrieval:

```python
await router.retrieve_work(
    work_id=work_id,
    work_type=work_type,
    operation=operation,
    backend=backend_ref,
)
```

Blocking retrieval:

```python
await router.retrieve_completed_work(
    work_id=work_id,
    work_type=work_type,
    operation=operation,
    backend=backend_ref,
    poll_interval_seconds=1.0,
    timeout_seconds=120.0,
)
```

It polls while status is:

```text
accepted
running
```

and returns when the backend returns:

```text
completed
failed
```

If the timeout is reached, RouterService returns a failed WorkResult with timeout metadata.

### 14.5 Batch send/retrieve

Submit many WorkPackets:

```python
batch_id = await router.send_many(work_packets)
```

For each packet, RouterService starts a normal JobManager job:

```text
kind = router.sendmany
runner = execute_send
```

Retrieve many WorkResults:

```python
retrieve_batch_id = await router.retrieve_many(results)
```

For each result:

```text
completed or failed -> passthrough job
accepted or running -> retrieval job
```

Completed and failed results are not retrieved again. They are passed through using:

```text
kind = router.retrievemany.passthrough
```

Accepted and running results use:

```text
kind = router.retrievemany
runner = execute_retrieve_completed
```

`execute_retrieve_completed()` requires:

```text
result.metadata["work_details"]
result.metadata["backend_ref"]
```

### 14.6 BackendClient

`BackendClient` is the internal HTTP client used by RouterService and backend-scoped command handlers.

It supports both full `BackendDescriptor` objects and serialized backend reference dictionaries.

Backend value access is normalized through:

```python
_backend_value(backend, key, default)
```

If `backend` is a dict, the value is read with `backend.get(key, default)`. Otherwise it is read with `getattr(backend, key, default)`.

Packet submission:

```python
await backend_client.submit_packet(
    backend=backend,
    packet=packet,
)
```

Work retrieval:

```python
await backend_client.retrieve_work(
    backend=backend_ref,
    work_id=work_id,
)
```

URL construction:

```text
<backend.url><backend.work_path>
<backend.url><backend.work_path>/<work_id>
```

The backend URL must start with `http://` or `https://`.

Generic helpers:

```python
await backend_client.get_json(url=..., params=...)
await backend_client.post_json(url=..., payload=...)
```

These helpers are used for backend health checks, model status checks, remote model loading, backend job status, and unload operations.

---

## 15. Federation architecture

Federation is the external membrane between Orin and the outside world.

External peers see only Federation-level concepts:

```text
FederationNetwork
NetworkMember
CapabilitySummary
FederatedWorkEnvelope
FederationWorkRecord
```

They must not see internal Orin implementation details.

### 15.1 Source layout

```text
src/federation/
  models.py
  settings.py
  storage.py
  network_registry.py
  join_tokens.py
  members.py
  identity.py
  envelopes.py
  policy.py
  app.py
  capabilities.py
  cortex_bridge.py
  federation_client.py
```

### 15.2 Runtime composition

During FastAPI lifespan startup, Federation:

1. loads the federation configuration file
2. creates an internal HTTP client
3. creates an external HTTP client
4. creates `MemoryFederationStorage`
5. loads or creates local cluster identity
6. creates `LocalNonceStore`
7. creates `NetworkRegistry`
8. creates `JoinTokenService`
9. creates `MemberService`
10. creates `CapabilityService`
11. creates `CortexBridge`

Persistent federation state is stored through Memory via `MemoryFederationStorage`. Local files are used for identity keys, nonce/cache state, and temporary local state.

### 15.3 Settings

`FederationSettings` is loaded from the shared file-based configuration system:

```python
configuration.load_configuration_file("federation")
configuration.get_configuration("federation")
```

Main settings:

```text
app_name = "orin-federation"
protocol_version = "v1"
host = "0.0.0.0"
port = 8080
cluster_id: str | None = None
data_dir = /data/federation
private_key_path: Path | None = None
public_key_path: Path | None = None
memory_base_url = http://memory-service:8080
memory_work_path = /work
public_base_url: str | None = None
cortex_base_url = http://cortex-service:8080
cortex_work_path = /work
cortex_work_result_path = /work/{work_id}
cortex_network_path = /network
default_network_visibility = public
default_join_mode = token
request_ttl_seconds = 300
allowed_clock_skew_seconds = 60
nonce_ttl_seconds = 600
max_request_bytes = 1_000_000
enable_remote_work_submission = true
enable_capability_publish = true
enable_member_heartbeat = true
```

Configuration may provide key paths, but not raw key material. If key paths are omitted, defaults are:

```text
/data/federation/identity/ed25519_private.key
/data/federation/identity/ed25519_public.key
```

### 15.4 Persistent storage

Federation does not own a separate SQLite database. Persistent federation records are stored through the existing Memory service.

Supported record types:

```text
network
join_token
member
work
```

Expected Memory operations:

```text
federation_put_record
federation_get_record
federation_list_records
federation_delete_record
```

Storage keys:

```text
network:    network_id
join_token: token_id
member:     <network_id>:<cluster_id>
work:       <network_id>:<work_id>
```

### 15.5 Local nonce store

`LocalNonceStore` is a local replay-protection cache. It is intentionally local file state, not persistent Memory state.

Default path:

```text
/data/federation/cache/nonces.json
```

Default TTL:

```text
600 seconds
```

`check_and_remember(nonce)` returns true when the nonce is new and false when it has already been seen.

### 15.6 Identity and signatures

Federation identity is Ed25519-based.

Public keys are encoded as:

```text
ed25519:<base64url>
```

Signatures are encoded as:

```text
ed25519:<base64url>
```

If `settings.cluster_id` is not configured, Federation derives a stable cluster ID from the public key:

```text
orin-<first_32_hex_chars_of_sha256(public_key_string)>
```

Canonical JSON for signatures:

```python
json.dumps(
    payload,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
).encode("utf-8")
```

Private key files are written with mode `0600`. Public key files are written with mode `0644`.

### 15.7 Networks

A `FederationNetwork` is the public concept that Orin clusters can discover and join.

```python
class FederationNetwork(BaseModel):
    network_id: str
    slug: str
    name: str
    description: str | None = None
    visibility: "public | unlisted | private" = "public"
    join_mode: "token | approval | open | closed" = "token"
    owner_cluster_id: str
    policy: NetworkPolicy
    advertised_capabilities: list[CapabilitySummary]
    member_count: int
```

Slug rules:

```text
3-64 characters
lowercase letters
numbers
hyphens
must start and end with a letter or number
```

`NetworkRegistry` owns network creation, lookup, listing, metadata updates, advertised capability updates, member-count refresh, deletion, and public-safe views.

### 15.8 Network policy

`NetworkPolicy` is default-deny and allow-list based.

Default allowed work types:

```text
llm
tool
```

Default allowed operations:

```text
chat
summarize
classify
extract
analyze
search
inspect
```

Policy fields include:

```text
allow_remote_work_submission
allow_remote_result_polling
allow_member_capability_publish
allowed_work_types
allowed_operations
max_payload_bytes
max_context_tokens
max_result_tokens
max_concurrent_jobs_per_member
expose_member_list
expose_exact_models
expose_runtime_metadata
```

There is no deny-list in the current model.

### 15.9 Join tokens

Join tokens are bootstrap credentials only.

Raw token format:

```text
orin_join_<token_id>.<secret>
```

Stored token records contain only a hash:

```text
token_hash = sha256(raw_token)
```

The raw token is returned once and must never be persisted.

`JoinTokenService` owns token creation, validation, redemption, revocation, expiry, deletion, and listing. It does not create `NetworkMember` records.

### 15.10 Members

A `NetworkMember` is a joined cluster identity inside a FederationNetwork.

```python
class NetworkMember(BaseModel):
    network_id: str
    cluster_id: str
    public_key: str
    role: "owner | admin | member | guest" = "member"
    status: "active | disabled | revoked | pending" = "active"
    allowed_work_types: list[str] = ["llm", "tool"]
    allowed_operations: list[str] = ["chat", "summarize", "analyze"]
    advertised_capabilities: list[CapabilitySummary]
```

Only active members can heartbeat, publish capabilities, or submit work.

A member can submit work only if:

```text
member.status == active
work_type in member.allowed_work_types
operation in member.allowed_operations
```

Both network policy and member policy must allow the requested work.

### 15.11 FederatedWorkEnvelope

External peers do not send raw internal WorkPackets directly. They send a signed envelope:

```python
class FederatedWorkEnvelope(BaseModel):
    federation_version: str = "v1"
    network_id: str
    origin_cluster_id: str
    target_cluster_id: str | None = None
    request_id: str
    issued_at: datetime
    expires_at: datetime
    nonce: str
    packet: dict[str, Any]
    signature: str
    signature_algorithm: str = "ed25519"
    metadata: dict[str, Any] = Field(default_factory=dict)
```

The `packet` field is intentionally `dict[str, Any]`, not a direct internal `WorkPacket` type. This keeps the Federation model layer decoupled from Cortex/common imports.

The envelope signature covers all envelope fields except `signature`. The signed payload still includes `signature_algorithm`.

Envelope validation checks:

```text
federation version
signature algorithm
network_id, when expected
origin_cluster_id, when expected
issued_at / expires_at timing
signature
nonce replay, when nonce_store is provided
```

Envelope validation does not perform membership lookup, policy enforcement, WorkPacket validation, quota checks, or routing.

### 15.12 Policy checks and packet sanitization

Known runtime operations:

```text
chat
summarize
classify
extract
analyze
search
inspect
```

Federation policy checks include:

```text
remote work submission is enabled
task object exists
task.work_type exists and is a string
task.operation exists and is a string
operation is a known runtime operation
work type is allowed by network policy
operation is allowed by network policy
payload size is within network/app limits
requested context limit is within policy
requested result-token limit is within policy
member is active
member is allowed to submit the work type
member is allowed to submit the operation
```

Before a federated packet reaches Cortex, Federation removes dangerous or trusted-looking metadata keys:

```text
backend_ref
backend_desc
selected_backend
internal
trusted
admin
command
deployment
kubernetes
ssh
```

Then it adds explicit federation-origin metadata:

```python
metadata["federation"] = {
    "network_id": network.network_id,
    "network_slug": network.slug,
    "origin_cluster_id": member.cluster_id,
    "member_role": member.role,
    "request_id": request_id,
}
```

Cortex should treat this as external-origin context, not trusted internal state.

### 15.13 Capability summaries

`CapabilityService` builds sanitized public Federation capability summaries from local Cortex discovery state.

It reads:

```text
settings.cortex_network_url()
```

It accepts several Cortex network response shapes:

```text
[backend, backend]
{"result": [...]}
{"backends": [...]}
{"network": [...]}
{"items": [...]}
{"descriptors": [...]}
{"llm": [...], "tool": [...]}
```

Only public work types are advertised:

```text
llm
tool
```

Multiple backend descriptors are merged into one public summary per work type so Federation does not reveal internal pod/service counts.

Capability summaries may include:

```text
work_type
operations
modalities
max_context_hint
max_result_tokens_hint
availability_hint
latency_hint
```

Exact model names are included only if `expose_exact_models=true`.

Runtime metadata is included only if `expose_runtime_metadata=true`, and only as coarse safe hints.

### 15.14 CortexBridge

`CortexBridge` bridges validated and sanitized Federation work into local Cortex.

Expected Cortex contract:

```text
POST /work
GET  /work/{work_id}
```

Expected response shape:

```json
{
  "status": "accepted",
  "work_id": "...",
  "content": [],
  "backend_name": "...",
  "backend_model": "...",
  "error": null,
  "metadata": {}
}
```

Supported Cortex statuses:

```text
accepted
running
completed
failed
```

`CortexBridge` creates a `FederationWorkRecord` and keeps separate IDs:

```text
origin_work_id
  Original WorkPacket id sent by the remote member.

cortex_work_id
  Internal Cortex job/work id used for local polling.

work_id
  Public Federation polling id returned to the remote member.
```

Current Phase 1 behavior: the public federation `work_id` is the Cortex work id. Later this can become an independent `fedwork:<id>` value.

### 15.15 FederationClient

`FederationClient` is the outbound client for talking to another Federation node. It only talks to Federation endpoints.

It must not talk directly to:

```text
Cortex
LLM
Memory
Tool
Kubernetes
backend services
```

Main methods:

```text
list_networks()
get_network()
get_network_capabilities()
join_network()
send_heartbeat()
publish_capabilities()
submit_work()
get_work_result()
submit_work_and_poll_once()
```

### 15.16 Current Federation flow

Current supported flow:

1. Create a FederationNetwork.
2. Create a join token.
3. Another cluster joins with token, cluster ID, and public key.
4. Member sends signed heartbeat.
5. Member publishes signed sanitized capabilities.
6. Member submits a signed FederatedWorkEnvelope.
7. Receiving Federation verifies envelope, membership, nonce, and policy.
8. Federation sanitizes the packet.
9. Federation submits the packet to local Cortex through CortexBridge.
10. Federation stores the FederationWorkRecord through Memory.
11. Remote member polls result through Federation.
12. Federation refreshes accepted/running records from Cortex and returns result or error.

### 15.17 Current Federation implementation notes

- Local/admin endpoints for creating networks and join tokens are currently unprotected. Later they should be restricted to the local command interface or explicit admin policy.
- Quota and concurrency enforcement are represented in policy fields but are not fully implemented in the policy layer yet.
- Federated backend descriptors for normal Cortex routing are still a later phase. Current remote work submission goes through explicit Federation endpoints and CortexBridge.
- Federation settings intentionally do not include bootstrap peers in the current phase. Bootstrap/relay discovery belongs to a later phase.

---

## 16. Primer and model execution

`Primer` is the shared runtime facade used by Cortex and the LLM app.

It supports:

- backend loading: `gguf` or `vllm`
- model provider loading: currently Hugging Face
- model file download/ensure logic
- model loading into backend
- OpenAI-style message adapter loading
- token counting
- text chat
- JSON chat
- streaming text

Primer uses canonical `RuntimeMessage` internally and renders messages through an adapter before sending them to the backend.

### 16.1 Message adapter

The current adapter is `OpenAIStyleMessageAdapter`.

It renders:

- text parts into text
- JSON parts into text
- image parts into image blocks
- unsupported parts into textual placeholders

When `nothink=True`, it injects:

```text
Do not output reasoning, chain-of-thought, or <think> tags. /no_think
```

If a model still emits `<think>...</think>`, the adapter splits that into an internal reasoning message and a user-visible final message.

### 16.2 Generation policies

Current token policy:

```text
chat:      384
summarize: 640
classify:  64
extract:  128
analyze:  900
search:   256
inspect:  256
```

Current temperature policy:

```text
chat:      0.7
summarize: 0.3
classify:  0.1
extract:   0.1
analyze:   0.4
search:    0.2
inspect:   0.1
```

`SAFETY_TOKEN_SIZE` is currently `64`.

---

## 17. LLM backend lifecycle

Typical LLM lifecycle:

```text
POST /engine {"engine":"gguf"}
POST /load   {"model_id":"...", "repo_id":"...", "filename":"...", "tokenizer_id":"..."}
GET  /jobs/{job_id}
POST /work   WorkPacket
GET  /work/{work_id}
POST /unload
```

The LLM backend requires Primer readiness before accepting work. If Primer is not ready, `/work` returns HTTP 409 with `primer_not_ready`.

LLM `/model_status` returns useful routing metadata after a model is loaded, including effective context size. Discovery probing uses this to populate:

```text
BackendDescriptor.runtime.effective_n_ctx
```

---

## 18. Memory backend

### 18.1 Memory operations

Memory supports operations such as:

```text
create_summary
list_summaries
create_note
list_notes
create_memory_event
list_memory_events
create_memory_claim
list_memory_claims
search_memory_claims
retrieve
prompt_context
upsert_user
create_session
resolve_session
create_message
list_recent_messages
federation_put_record
federation_get_record
federation_list_records
federation_delete_record
```

The Federation operations are used by `MemoryFederationStorage` for persistent Federation state.

### 18.2 Persistence

Default memory base directory:

```text
./cluster-memory
```

Default storage paths:

```text
cluster-memory/memory.db
cluster-memory/notes/
cluster-memory/summaries/
cluster-memory/docs/
cluster-memory/yaml/
```

### 18.3 SQLite tables

Memory creates tables such as:

```text
users
chat_sessions
chat_messages
session_summaries
note_index
memory_events
memory_claims
work_items
```

### 18.4 Prompt context

The `prompt_context` operation returns:

```text
recent_messages
claims
summaries
retrieved_chunks
prompt_block
```

Cortex uses this for interpretation, shaping, last-assistant-message retrieval, and final response building.

### 18.5 Session behavior

`resolve_session` uses inactivity-based rollover:

1. find latest active session for user/channel
2. if none exists, create a session
3. if the latest active session is older than `max_idle_minutes`, close it and create a new one
4. otherwise reuse the active session

Default maximum idle time from Cortex is currently 60 minutes.

---

## 19. Terminal client

`terminal-chat.py` is a lightweight local terminal client for Cortex.

Default target:

```text
http://orin-gw:30080/work
```

Default user:

```text
terminal-user
```

The client sends canonical `InferenceSession` payloads using `RuntimeMessage` content.

On startup it sends:

```text
/render
```

It separates command output from chat output by checking the response header:

```text
Terminal-output: terminal
```

If that header is present, output goes into the command panel. Otherwise output is treated as assistant chat.

For `/attach`, the terminal client should catch Ctrl+C in streaming mode and return to the terminal loop instead of exiting the whole client.

---

## 20. Operations runbook

### 20.1 Bootstrap

Run the installer from the bootstrap/control host:

```bash
./install-orin.sh
```

Useful environment overrides:

```bash
ORIN_NAMESPACE=orin \
HOST_ALIAS=orin-gw \
HOST_NAME=orin-gw \
HOST_IP=<host-ip> \
PLATFORM=jetson \
NVIDIA_GPU=true \
REGISTRY=orin-gw:5000 \
IMAGE_VERSION=1.0.0 \
./install-orin.sh
```

### 20.2 Health checks

```bash
kubectl get nodes -o wide
kubectl get pods -n orin -o wide
kubectl get svc -n orin
kubectl get deployments -n orin
```

Cortex:

```bash
curl -s http://orin-gw:30080/health
curl -s http://orin-gw:30080/ready
curl -s http://orin-gw:30080/network
```

Memory inside cluster:

```bash
kubectl exec -n orin netdebug -- sh -lc 'curl -s http://memory-service:8080/health'
```

### 20.3 Debug pod

```bash
kubectl run netdebug \
  -n orin \
  --image=nicolaka/netshoot \
  --restart=Never \
  --command -- /bin/sh -c "sleep 86400"
```

### 20.4 Terminal client

```bash
python terminal-chat.py
```

Initial command menu:

```text
/render
```

Useful command sequence:

```text
/commands
/cd Deployment
/list_nodes
/list_pods
```

### 20.5 Add and install a node

Add node to inventory:

```text
/add_node orin-node-1 --host orin-node-1 --ip 192.168.50.11 --platform jetson --gpu
```

Install k3s agent:

```text
/install_k3s orin-node-1
```

Sync labels and add role preference:

```text
/label_node orin-node-1 --role llm
```

List nodes:

```text
/list_nodes --verbose
```

### 20.6 Deploy an LLM pod

```text
/deploy_pod llm light --platform jetson
```

or pin to a node:

```text
/deploy_pod llm light --node orin-node-1
```

List pods:

```text
/list_pods --verbose
```

### 20.7 Load a model

Cortex-local model load from the Configuration folder:

```text
/cd /
/cd Configuration
/model_aliases
/load_model qwen35-4b
/job_status <job_id>
/attach
```

LLM backend model load from a selected backend folder:

```text
/cd /
/cd Cluster
/cd <llm-backend>
/model_status
/load_model qwen35-4b
/job_status <job_id>
/attach
```

For Hugging Face exploration:

```text
/cd /
/cd Configuration
/cd ModelDownloader
/huggingface search qwen
/huggingface gguf unsloth/Qwen3.5-9B-GGUF
```

### 20.8 Federation manual test outline

A minimal Federation test flow:

```text
create network
create join token
join network with token, cluster_id, public_key
send signed heartbeat
publish signed capabilities
submit signed FederatedWorkEnvelope
poll federated work result
```

Exact curl examples should be documented separately once the command-router Federation folder or test script is finalized.

---

## 21. Known implementation notes and cleanup

Current known cleanup items:

1. The `get_job_result` branches in Harness should either be added to the lightweight interpretation model or removed.
2. `RouterService.retrieve_work()` treats `backend` contractually as a dict but some error metadata still uses object-style `getattr`.
3. `summarize_jobs_for_prompt()` includes payload details for completed jobs; this should become a compact result summary.
4. Some storage profile naming may still reference `strong` while the active pod template uses `high`.
5. Image naming should be cleaned up so `amd64` and `arm64` share the standard runtime image while Jetson keeps its own image.
6. `PodImageConfig.image_for()` should treat `arm64` and `amd64` as separate platform values, not as one combined string.
7. `DeploymentService._build_pod_factory_defaults()` should read registry/version from configuration instead of hardcoding defaults.
8. Routed streaming WorkPackets are not fully aligned if a backend returns a streaming response where RouterService expects JSON.
9. `MemoryRuntime.handle_egress()` may remain a placeholder when memory work completes synchronously on POST.
10. Several memory error strings still say `Summary Instance` for other request types.
11. Mutable Pydantic defaults should consistently use `Field(default_factory=...)`.
12. Direct `/load` for GGUF should consistently pass or infer the Hugging Face provider when `repo_id` and `filename` are supplied.
13. Federation local/admin endpoints for network and token creation need local admin protection or command-router-only access.
14. Federation quota/concurrency fields exist in policy but are not fully enforced yet.
15. Federated backend descriptors for normal Cortex routing are a later phase.
16. Tool backend implementation remains future work.
17. Deployment inventory is still in-memory and should eventually be persisted.

Removed stale items from older drafts:

```text
Federation as an empty/planned folder
old /task pipeline as the current background mechanism
memoryrequest field spelling
backend /network command as the current selected-backend display command
orin.ai/health_path, orin.ai/work_path, orin.ai/models_path as active discovery annotations
LLM /models as the active model-status discovery endpoint
accepted_not_executed Federation placeholder as current behavior
```

---

## 22. Near-term architecture direction

Next architecture cleanup should focus on:

1. keeping WorkPacket as the single execution unit across Cortex, LLM, Memory, Tool, and Federation paths
2. stabilizing Harness lightweight interpretation and background pipeline behavior
3. completing the standard-vs-Jetson image split
4. making service discovery metadata patching part of normal model/runtime operations
5. adding the first real Tool backend
6. protecting local/admin Federation endpoints
7. enforcing Federation quota/concurrency policy
8. adding command-router support for Federation operations
9. persisting deployment inventory beyond current in-memory inventory
10. replacing any remaining old gateway/orchestrator terminology with Cortex/LLM/Memory/Tool/Federation terminology

The final direction is a public, no-pay overlay network where Orin installs can discover FederationNetworks, join authorized networks with tokens, and exchange signed policy-limited WorkPackets without exposing internal cluster structure.
