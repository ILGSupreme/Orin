# Orin Cluster - Current Architecture and Operations

Author: Samuel Padilla  
Scope: Current-state architecture and day-to-day operations for the Orin AI cluster  
Status: First draft based on the current `install-orin.sh`, Cortex, LLM, memory, discovery, deployment, and terminal client code

---

## 1. Purpose

The Orin Cluster is a Kubernetes-based local edge-AI assistant platform. The current design is centered on **Cortex** as the local reasoning and routing process, with runtime work represented as canonical **WorkPackets** and routed to discovered backend services.

The current implementation is no longer best described as a gateway/orchestrator/Ollama split. The active shape is:

- **Cortex**: ingress, command interface, deployment operations, memory/session integration, local fast response, discovery, routing, and future complex-task handling.
- **LLM**: a Primer-backed WorkPacket executor for model inference.
- **Memory**: a WorkPacket backend for users, sessions, chat history, claims, notes, summaries, and prompt context.
- **Tool**: planned backend role; folder exists but is currently empty.
- **Federation**: planned inter-cluster role; folder exists but is currently empty.
- **Common**: shared protocol, runtime, model-loading, adapter, and Hugging Face helpers.

The current runtime direction is: **everything executable should become a WorkPacket**, and anything that needs routing should pass through the same router/discovery path, whether it is LLM work, memory work, tool work, or deferred Cortex work.

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

Notes:

- `common/` contains shared protocols, message adapters, Primer, Hugging Face helpers, and model/runtime types.
- `cortex/` contains the main Cortex app, command router, deployment service, Kubernetes discovery, routing, and mailbox code.
- `llm/` contains the Primer-backed LLM backend app. Its runtime class is currently named `CortexRuntime`, but this is temporary and should be renamed to an LLM-specific name such as `LLMRuntime`.
- `memory/` contains the memory backend app, SQLite persistence, file-backed memory, and prompt-context logic.
- `tool/` and `federation/` are present as planned runtime roles but are currently empty.

---

## 3. Image model

The intended image split is:

```text
standard runtime, amd64/arm64:
  FROM orin-gw:5000/primer-runtime:0.2

jetson runtime:
  FROM orin-gw:5000/primer-runtime:jetson-orin.0.2
```

The application folders `cortex`, `llm`, `memory`, and `tool` each have:

```text
Dockerfile
Dockerfile.jetson
```

The long-term image model should distinguish only between:

- standard runtime: `amd64` and `arm64`
- Jetson runtime: Jetson-specific base/runtime

Current implementation note: `install-orin.sh` and part of the deployment factory still contain a three-way platform image selection for `jetson`, `arm64`, and `amd64`. That should be cleaned up so `amd64` and `arm64` share the same standard image naming while Jetson keeps its separate image.

---

## 4. Bootstrap model: `install-orin.sh`

`install-orin.sh` is the initial host/bootstrap installer. It is not the long-term general pod deployment mechanism.

Its responsibilities are:

1. detect the host platform and GPU state
2. optionally configure the local test registry for k3s/containerd
3. install k3s server on the bootstrap/control host
4. create namespace and RBAC
5. create the Cortex SSH-key secret
6. create the k3s join-token secret
7. write the deployment configuration file
8. create the deployment configuration ConfigMap
9. label the bootstrap node
10. deploy the initial `memory` app
11. wait for memory rollout
12. deploy the initial `cortex` app
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

The configuration seeds the initial host/control node and deployment defaults. It is not the final source of truth for every future node. Other nodes are added later through Cortex's command interface and `DeploymentService`.

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

The installer deploys only the minimum working runtime:

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

### 4.5 Bootstrap RBAC

The Cortex service account needs permissions to manage cluster resources and discover backends.

The installer creates a `ClusterRole` and `ClusterRoleBinding` for:

- nodes: `get`, `list`, `watch`, `patch`, `update`
- namespaces: `get`, `list`, `create`
- pods, services, persistentvolumeclaims, configmaps: `get`, `list`, `watch`, `create`, `patch`, `update`, `delete`
- secrets: `get`, `list`, `watch`
- deployments: `get`, `list`, `watch`, `create`, `patch`, `update`, `delete`
- endpointslices: `get`, `list`, `watch`

EndpointSlice access is required because backend discovery counts ready endpoints from EndpointSlices.

---

## 5. Runtime applications

### 5.1 Cortex

Cortex is the main local runtime. It owns:

- public `/work` ingress
- slash-command handling
- local Primer-backed fast response path
- user/session/message persistence through memory WorkPackets
- backend discovery and registry refresh
- backend routing and WorkPacket submission
- deployment operations through `DeploymentService`
- future deferred/complex task handling

At startup, the Cortex app creates:

```text
Primer
DeploymentService
DiscoveryService
BackendRoutingPolicy
CommandRouter
BackendClient
Planner
RouterService
CortexMailbox
CortexRuntime
```

Cortex starts the discovery refresher on lifespan startup and stops the Primer backend on shutdown.

### 5.2 LLM

The LLM app is a Primer-backed WorkPacket executor. It exposes backend/model lifecycle endpoints and a packet execution API.

It supports:

- backend loading through `/backend`
- model loading through `/load`
- model unloading through `/unload`
- work submission through `/work`
- work polling through `/work/{work_id}`
- model status through `/models`

Cortex discovers LLM services through Kubernetes service metadata and routes `work_type=llm` packets to them.

### 5.3 Memory

The memory app is a synchronous WorkPacket backend. Cortex routes memory WorkPackets to it for:

- user upsert
- session resolution
- chat message creation
- recent conversation retrieval
- claims
- notes
- summaries
- prompt context

The memory backend returns completed `WorkResult` values directly from `POST /work`.

### 5.4 Tool

The `tool/` app folder exists but is currently empty. It represents the planned backend role for non-LLM tool execution.

### 5.5 Federation

The `federation/` app folder exists but is currently empty. It represents the planned inter-cluster communication role.

Federation should not be documented as currently working. It is a planned boundary for future trusted remote cluster discovery, delegation, and policy-controlled task exchange.

---

## 6. Runtime endpoints

### 6.1 Cortex endpoints

| Endpoint | Method | Purpose |
|---|---:|---|
| `/work` | POST | Main Cortex ingress for `InferenceSession`. |
| `/work/{work_id}` | GET | Reads deferred work state from `CortexMailbox`. |
| `/network` | GET | Lists discovered backend descriptors. |
| `/network/{role}` | GET | Lists discovered candidates for a role. |
| `/backend` | POST | Loads a local Primer backend for Cortex. |
| `/load` | POST | Loads a local model into Cortex's Primer. |
| `/unload` | POST | Stops the local Primer backend/model. |
| `/live` | GET | Process liveness. |
| `/ready` | GET | Readiness and Primer status. |
| `/health` | GET | Basic health endpoint. |

### 6.2 LLM endpoints

| Endpoint | Method | Purpose |
|---|---:|---|
| `/backend` | POST | Load `gguf` or `vllm` backend. |
| `/load` | POST | Load a model. |
| `/unload` | POST | Stop the current backend/model. |
| `/work` | POST | Accept a `WorkPacket`. |
| `/work/{work_id}` | GET | Poll a submitted LLM work item. |
| `/models` | GET | Return loaded model metadata including `effective_n_ctx`. |
| `/live` | GET | Process liveness. |
| `/ready` | GET | Readiness and Primer status. |
| `/health` | GET | Basic health endpoint. |

### 6.3 Memory endpoints

| Endpoint | Method | Purpose |
|---|---:|---|
| `/work` | POST | Execute memory `WorkPacket` operations synchronously. |
| `/work/{work_id}` | GET | Placeholder; memory work currently completes on POST. |
| `/live` | GET | Process liveness. |
| `/ready` | GET | Readiness. |
| `/health` | GET | Basic health endpoint. |

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
    memoryrequest: RuntimeMemoryRequest | None = None
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

---

## 8. Cortex runtime flow

### 8.1 Normal chat flow

For a normal `POST /work` request, Cortex currently does:

1. validate `InferenceSession`
2. convert it into an `InferenceObject`
3. force internal `stream=False` on the inference object
4. upsert user through memory
5. resolve or create session through memory
6. store the incoming user message through memory
7. detect slash commands
8. if not a command, require local Primer readiness
9. fetch last assistant message through memory
10. build fast response prompt
11. call local Primer with chat settings
12. return `EgressResponse` or stream text

The current fast response path uses Cortex's own local Primer. More complex routing happens through WorkPackets and the router.

### 8.2 Slash command flow

If the latest user text starts with `/`, Cortex treats it as a terminal command.

Command output is returned as terminal output using:

```text
Terminal-output: terminal
```

The terminal client uses that header to decide whether the output belongs in the command panel or in the chat transcript.

### 8.3 Current `/task` behavior

The current `/task <description>` command starts a background `_run_pipeline()` task in `CortexRuntime`.

That pipeline performs:

```text
prompt context
-> turn interpretation
-> capability summary
-> task shaping
-> WorkPacket creation
-> packet submission
-> sync/deferred result collection
-> final response packet
-> final result storage
-> assistant message insertion into memory
```

This is useful as a prototype, but it is not yet unified with `CortexMailbox`.

### 8.4 Direction for deferred and complex tasks

Current design direction:

> Deferred and complex tasks should use the same WorkPacket pipeline used for LLM, tool, and memory work. Cortex should create a WorkPacket and route it to the closest/best Cortex backend. In the current local cluster, that will usually be the initial Cortex.

The separate `/task` pipeline and `CortexMailbox` deferred mechanism are implementation debt to unify later.

---

## 9. Command interface

The command router exposes a folder-style terminal interface.

Current folder tree:

```text
Root
  /help
  /commands
  /cd
  /network

  Deployment
    /deploy_pod
    /delete_pod
    /list_pods
    /install_k3s
    /add_node
    /remove_node
    /list_nodes
    /label_node

  Disconnect
    /disconnect
    /reset

  Configuration
    /models
    /load_model
    /unload_model
    /backend

    ModelDownloader
      /huggingface
```

Important command behavior:

- `/render`, `/commands`, `/help`, and `/cd` are effectively global.
- Other commands are only available when visible in the current command folder.
- Streaming command output is returned as text/plain with `Terminal-output: terminal`.
- Non-stream command output is wrapped in an `EgressResponse` with terminal metadata.

### 9.1 Model commands

Model aliases currently include:

```text
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

## 10. Deployment service and inventory

`DeploymentService` owns the mutable in-memory `Inventory`. It reads `/data/configuration` during startup and seeds the initial host/control node into inventory.

### 10.1 Inventory model

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

### 10.2 DeploymentService responsibilities

`DeploymentService` supports:

- adding/removing inventory nodes
- listing inventory nodes with Kubernetes status
- labeling Kubernetes nodes
- checking passwordless sudo over SSH
- installing k3s server/agent over SSH
- deploying pod apps through Kubernetes Python client
- deleting pod apps
- listing deployed pod apps
- patching service discovery metadata
- resolving pod placement

### 10.3 Node placement labels

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

### 10.4 Service discovery labels

Service labels and annotations are used for backend discovery and routing:

```text
labels:
  orin.ai/backend=true
  orin.ai/role=<role>

annotations:
  orin.ai/kind=<kind>
  orin.ai/role=<role>
  orin.ai/visibility=internal
  orin.ai/health_path=/health
  orin.ai/work_path=/work
  orin.ai/models_path=/models        # llm only
```

Runtime metadata can be patched later:

```text
orin.ai/model
orin.ai/capabilities
orin.ai/modalities
```

---

## 11. Pod application model

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

Cortex is deployed as NodePort. LLM, memory, and tool apps are deployed as ClusterIP services.

Current probe convention:

```text
readiness: /ready
liveness:  /live
startup:   /ready
```

Current storage profile implementation in `pod_template.py`:

```text
light  -> 20Gi
medium -> 50Gi
high   -> 100Gi
```

Current cleanup note: some command/type definitions still use `strong` instead of `high`.

---

## 12. Discovery, health, and routing

### 12.1 DiscoveryService

Cortex creates a `DiscoveryService` consisting of:

```text
KubernetesDiscoveryProvider
BackendHealthProber
InMemoryBackendRegistry
RegistryRefresher
BackendSelector
```

The registry refreshes every 15 seconds by default.

### 12.2 Discovery source

Backends are discovered from Kubernetes Services in the configured namespace with:

```text
orin.ai/backend=true
```

The service must also provide:

```text
orin.ai/role=<role>
```

EndpointSlice readiness is used to count ready endpoints.

### 12.3 BackendDescriptor

Discovered services become `BackendDescriptor` objects containing:

```text
name
namespace
service_name
url
role
kind
model
capabilities
modalities
context_window
max_output_tokens
priority
weight
visibility
health_path
work_path
models_path
labels
annotations
health
runtime.effective_n_ctx
```

### 12.4 Health probing

Health probing works as follows:

1. if no ready EndpointSlice endpoints exist, backend is `unavailable`
2. otherwise GET `<url><health_path>`
3. if health fails, backend is `degraded`
4. if `models_path` exists, GET `<url><models_path>`
5. if model check succeeds, read `models[0].effective_n_ctx` into runtime metadata
6. if checks pass, backend is `healthy`

### 12.5 Backend selection

Backend selection filters by:

- health
- runtime preference, such as required `effective_n_ctx`
- role
- required capabilities
- required modalities

Then it sorts by:

1. healthy first
2. priority descending
3. weight descending
4. name ascending

Routing is healthy-first with degraded fallback.

### 12.6 RouterService

`RouterService.execute(packet)` handles packet dispatch.

If `packet.work_type == cortex`, the router sends it to the local `CortexMailbox`.

Otherwise it:

1. resolves a backend through the planner/routing policy
2. POSTs the WorkPacket to `<backend.url><backend.work_path>`
3. if the backend returns `completed` or `failed`, uses that result immediately
4. otherwise polls `<backend.work_path>/<work_id>` until a completed result is available

---

## 13. Primer and model execution

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

Primer uses the canonical `RuntimeMessage` format internally and renders messages through an adapter before sending them to the backend.

### 13.1 Message adapter

The current adapter is `OpenAIStyleMessageAdapter`.

It renders:

- text parts into text
- json parts into text
- image parts into image blocks
- unsupported parts into textual placeholders

When `nothink=True`, it injects:

```text
Do not output reasoning, chain-of-thought, or <think> tags. /no_think
```

If a model still emits `<think>...</think>`, the adapter splits that into an internal reasoning message and a user-visible final message.

### 13.2 Generation policies

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

## 14. LLM backend lifecycle

Typical LLM lifecycle:

```text
POST /backend {"backend":"gguf"}
POST /load    {"model_id":"...", "repo_id":"...", "filename":"...", "tokenizer_id":"..."}
POST /work    WorkPacket
GET  /work/{work_id}
POST /unload
```

The LLM backend requires Primer readiness before accepting work. If Primer is not ready, `/work` returns HTTP 409 with `primer_not_ready`.

LLM `/models` returns useful routing metadata after a model is loaded:

```json
{
  "ok": true,
  "models": [
    {
      "id": "...",
      "backend": "gguf",
      "effective_n_ctx": 4096,
      "n_gpu_layers": -1,
      "n_batch": 512,
      "runtime_profile": "balanced"
    }
  ]
}
```

This is used by discovery probing to populate `BackendDescriptor.runtime.effective_n_ctx`.

---

## 15. Memory backend

### 15.1 Memory operations

Memory supports these WorkPacket operations:

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
```

### 15.2 Persistence

Default memory base directory:

```text
./cluster-memory
```

Default storage:

```text
cluster-memory/memory.db
cluster-memory/notes/
cluster-memory/summaries/
cluster-memory/docs/
cluster-memory/yaml/
```

### 15.3 SQLite tables

Memory currently creates:

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

### 15.4 Prompt context

The `prompt_context` operation returns:

```text
recent_messages
claims
summaries
retrieved_chunks
prompt_block
```

Cortex uses this for interpretation, shaping, and last-assistant-message retrieval.

### 15.5 Session behavior

`resolve_session` uses inactivity-based rollover:

1. find latest active session for user/channel
2. if none exists, create a session
3. if the latest active session is older than `max_idle_minutes`, close it and create a new one
4. otherwise reuse the active session

Default maximum idle time from Cortex is currently 60 minutes.

---

## 16. Terminal client

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

---

## 17. Operations runbook

### 17.1 Bootstrap

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

### 17.2 Health checks

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

### 17.3 Debug pod

```bash
kubectl run netdebug \
  -n orin \
  --image=nicolaka/netshoot \
  --restart=Never \
  --command -- /bin/sh -c "sleep 86400"
```

### 17.4 Terminal client

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

### 17.5 Add and install a node

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

### 17.6 Deploy an LLM pod

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

### 17.7 Load a model

Inside the Configuration folder:

```text
/cd ..
/cd Configuration
/backend gguf
/load_model qwen35-4b
/models
```

For Hugging Face exploration:

```text
/cd ModelDownloader
/huggingface search qwen
/huggingface gguf unsloth/Qwen3.5-9B-GGUF
```

---

## 18. Known implementation cleanup

Current known cleanup items:

1. The `/task` command pipeline and `CortexMailbox` deferred pipeline should be unified through WorkPackets routed to the closest/best Cortex backend.
2. The LLM runtime class is currently named `CortexRuntime`; rename it to `LLMRuntime` or similar.
3. `tool/` and `federation/` are empty and should be documented only as planned roles.
4. Image naming should be cleaned up so `amd64` and `arm64` share the standard runtime image while Jetson keeps its own image.
5. `PodImageConfig.image_for()` should treat `arm64` and `amd64` as separate values in a set, not as a single combined string.
6. `DeploymentService._build_pod_factory_defaults()` should read registry/version from the configuration file instead of hardcoding `orin-gw:5000` and `1.0.0`.
7. Storage profile naming is inconsistent: `pod_template.py` uses `high`, while other command/type code still references `strong`.
8. Routed streaming WorkPackets are not fully aligned yet because `RouterService.retrieve_work()` expects JSON but LLM streaming can return `StreamingResponse`.
9. `MemoryRuntime.handle_egress()` is currently a placeholder because memory work completes synchronously on POST.
10. `memory.sql` contains work item persistence helpers, but they are not yet part of the active WorkPacket memory contract.
11. Several memory error strings still say `Summary Instance` even for other request types.
12. Some mutable Pydantic defaults should be changed to `Field(default_factory=dict)`.
13. Direct `/load` for GGUF should consistently pass or infer the Hugging Face provider when `repo_id` and `filename` are supplied.
14. Router polling should eventually handle `failed` during polling and use a timeout.
15. `Planner.derive_requirements()` and `Planner.select_backend()` should be aligned so routing requirements are consistently derived from WorkPackets.

---

## 19. Near-term architecture direction

The next architecture cleanup should focus on:

1. making WorkPacket the single execution unit across Cortex, LLM, memory, tool, and deferred work
2. unifying `/task` and `CortexMailbox`
3. finishing the standard-vs-Jetson image split
4. making service discovery metadata patching part of normal model/runtime operations
5. adding the first real tool backend
6. defining the federation boundary only after local WorkPacket routing is stable
7. persisting deployment inventory beyond the current in-memory inventory
8. replacing old gateway/orchestrator terminology with Cortex/LLM/memory/tool/federation terminology throughout the code and documentation
