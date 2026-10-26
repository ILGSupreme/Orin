# Orin

Owner and maintainer: [Samuel Padilla](https://github.com/ILGSupreme).

Orin is a distributed assistance platform for connecting users with agents,
generative models and executable capabilities across local, edge and federated
environments. Its purpose is to make useful capabilities accessible even when
the user's device lacks the necessary hardware, models, software or data access.

The current implementation targets Kubernetes, with k3s as the practical lab
environment and Jetson and standard Linux machines as deployment targets.
**Orin is experimental:** the platform vision is broader than the capabilities
currently implemented or validated.

## Architecture and responsibilities

| Component | Responsibility |
| --- | --- |
| Cortex | User-facing assistant and controller: conversation, goals, execution, observations, feedback, discovery and routing. It also provides explicit local administration commands. |
| Inference services | Execute generative models through replaceable backends. Primer supports Cortex-local inference and separate LLM services; the inference gateway also supports configured cluster and external-provider routes. |
| Tool capabilities | Perform defined operations against systems, data or hardware. Built-in read-only inspection lives inside Cortex; additional tools require real implementations and configuration. |
| Memory | Persist conversation, operational state, goals, observations, recovery information, node inventory and Federation records. |
| Federation | Connect independently administered environments through network discovery, membership, signatures, policy checks and work/result exchange. |
| Common | Shared protocols, runtime messages, WorkPackets, model adapters, Primer and job infrastructure. |

A node is a deployment host and may provide several services. Ordinary inference
and tool execution do not require another agent. Remote agents that independently
plan and coordinate delegated outcomes remain a later step.

Capability discovery, authorization and execution are separate concerns.
Discovering a service does not authorize its use, and local agent permissions do
not transfer across a Federation boundary.

## Cortex and persistent work

Natural requests through `/chat` and `/terminal_chat` share one agent runtime.
A turn can produce an immediate conversational reply and persistent tasks without
requiring the user to select a separate agent mode.

The implemented runtime includes task dependencies, bounded execution,
observations, clarification, corrective feedback, cancellation and restart
recovery. Memory stores goals with revision checks and expiring execution leases.
Completion requires references to successful observations; the model still
assesses whether those observations satisfy the requested outcome.

Explicitly requested schedules and local events can wake bounded runs. Results
and notices are persisted and available through goal polling and the terminal
client. External notification delivery requires separate capabilities.

Inference bindings select interpretation, decision and work routes independently.
External inference is disabled by default and requires both operator configuration
and permission to send the turn or goal's context externally.

See [the Cortex runtime](docs/cortex-agent.md) for configuration, permissions,
budgets, supported inputs and recovery semantics.

## Inference and tools

Primer is the shared local model runtime facade used by Cortex and LLM services.
It handles model loading, adapters, token counting and generation through engines
such as GGUF and vLLM. Separate LLM services are discovered through Kubernetes
service metadata and selected according to health, capabilities, modalities and
routing policy. Cortex can also use its local model; a separate LLM pod is not
required for every deployment.

Built-in agent actions include local status, node and workload inspection,
analysis, summarization and input description. Their usefulness depends on actual
permissions, inventory and configured inference routes. Search and other
retrieval operations require a healthy tool backend implementing the advertised
contract; model reasoning alone does not retrieve current information.

There is no complete general-purpose Tool service bundled with the current
runtime. A capability may be implemented within Cortex or as a separate service;
it does not have to be a separate application or agent.

## Federation

Federation has an implemented HTTP MVP, including:

- Public network listing and manifests, with token-gated joining.
- Ed25519 identities, signed work envelopes, nonce and expiry checks.
- Signed member heartbeats and capability publication.
- Network and member policy checks, incoming packet sanitization and result
  sanitization.
- Explicit work submission through CortexBridge and result polling, with
  persistent records stored through Memory.

The supplied June 5, 2026 lab checkpoint records a completed signed
submit/poll flow with sanitized inference output. This is historical evidence;
it does not establish that the current revision or deployment passes that flow.

Cortex's direct `/work` ingress currently accepts LLM work only. Federation
membership grants no Cortex goal controls, Memory access, arbitrary terminal
commands or local administration permissions.

Automatic selection of federated backends through normal Cortex routing,
decentralized public discovery and independent remote-agent delegation remain
future work. Local network listing is not global peer discovery. Public exposure
also requires review of administrative endpoint protection, polling authorization
and quota/concurrency enforcement.

See the [Federation network plan](docs/orin-federation-network-plan.md) for
design direction; its proposed phases and transports are not all implemented.

## Runtime protocol and persistence

Orin uses canonical RuntimeMessages, WorkPackets and WorkResults internally.
WorkPackets describe executable backend work with typed tasks, routing hints and
metadata. RouterService provides shared submission and retrieval for discovered
backends; JobManager tracks transient execution jobs. Durable goals and
responsibilities belong to Memory.

Runtime message types can represent text, images, audio, video, JSON and binary
data. Representation does not imply that every backend or ingress supports every
modality. Current provider and agent input limitations are documented in
[the Cortex runtime](docs/cortex-agent.md).

Memory also persists worker node configuration separately from conversation data.
See [node inventory](docs/node-inventory-api.md) and
[storage upgrades](docs/memory-storage-upgrade.md) before upgrading an existing
installation.

## Deployment and terminal client

`install_orin.sh` bootstraps the initial k3s host, namespace, RBAC, configuration,
secrets, storage and Memory, Cortex and Federation services. Cortex deployment
commands support adding worker inventory, installing k3s agents, labeling nodes
and deploying runtime services.

These operations affect hosts and running services. Review the installer and
configuration for the target environment, registry, storage and GPU runtime.
Jetson-specific images and native dependencies require hardware validation.

`terminal-chat.py` defaults to `/terminal_chat` and supports natural requests,
explicit slash commands and polling for goal results and questions.
`ORIN_SESSION_ID` resumes a known session; `ORIN_PRIVACY` selects the turn's
inference privacy policy. Endpoint and connection settings are configurable.

Cortex and Memory interfaces are local administrative/internal interfaces.
Federation is the intended cross-environment boundary; internal service URLs,
credentials and cluster details must remain private.

## Repository structure

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
  install_orin.sh
  terminal-chat.py
```

The Tool role exists in routing and configuration; a standalone `src/tool/`
implementation is not currently present.

## Validation and current priorities

Pull requests run CPU tests and focused static checks on hosted runners for
Python 3.10 and 3.12. A separate native GGUF grammar check compiles the agent
schemas with a CPU llama.cpp parser without loading model weights.

Tests exercise real Memory/SQLite and runtime logic with model, provider HTTP
and tool doubles. Kubernetes discovery tests also use a local HTTP server.
These checks do not establish successful deployment, sound model decisions,
Jetson/CUDA compatibility or end-to-end task completion. Live Federation smoke
tests are separate from unit-test discovery.

See [CI and local test instructions](docs/ci.md) for commands and check scope,
[lab access](tools/lab-mcp/README.md) for the lab bridge, and
[standalone dev-agent](https://github.com/ILGSupreme/dev-agent) for development
workflows. Development coordination is maintained in that separate repository.

Current priorities are:

- Prove a reliable Cortex execution and feedback loop with real services,
  persisted observations and completion evidence.
- Evaluate representative requests repeatedly with the actual small models and
  deployed inference configuration.
- Validate current images, dependencies, inventory recovery and Kubernetes
  operations on the gateway and Jetson.
- Add useful real tools and harden Federation's trust boundary before broader
  public use.
- Introduce automatic federated routing and agent delegation incrementally.

The [agent action plan](docs/unified-agent-action-plan.md) and
[architecture and operations reference](orin-cluster-current-architecture-and-operations.md)
provide additional context. Older architecture notes may describe superseded
flows; use current code and focused runtime documentation to resolve differences.

## License

Orin source code is licensed under the Apache License, Version 2.0.
See [LICENSE](LICENSE).

Model weights, third-party containers, runtime libraries and external services
may have separate licenses. See [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
