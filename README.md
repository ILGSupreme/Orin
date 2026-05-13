# Orin

Orin is a local edge-AI cluster runtime for Kubernetes, developed by Nordavind.

It is designed for running local AI workloads across edge machines, including
Jetson devices and standard Linux nodes. The current architecture is centered
around Cortex, a local ingress, routing, command, discovery, and orchestration
process.

Orin uses a canonical WorkPacket protocol for routing executable work between
runtime services such as LLM, memory, tool, and future federation backends.

## Current architecture

The current runtime roles are:

- Cortex: ingress, command interface, deployment operations, memory/session
  integration, local fast responses, discovery, routing, and future complex-task
  handling.
- LLM: Primer-backed WorkPacket executor for model inference.
- Memory: WorkPacket backend for users, sessions, chat history, claims, notes,
  summaries, and prompt context.
- Tool: planned backend role for non-LLM tool execution.
- Federation: planned inter-cluster role for trusted remote cluster delegation.
- Common: shared protocol, runtime, model-loading, adapter, and Hugging Face
  helper code.

The design direction is that executable runtime work should be represented as
WorkPackets and routed through a shared discovery/routing path.

## Repository structure

```text
orin/
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

## Main components

### Cortex

Cortex is the main local runtime. It owns public ingress, slash-command handling,
deployment operations, backend discovery, routing, and memory/session integration.

Cortex is responsible for receiving user requests, managing terminal commands,
coordinating memory/session state, discovering backend services, and routing
runtime work.

### LLM

The LLM service is a Primer-backed runtime that can load model backends such as
GGUF or vLLM and execute WorkPackets for model inference.

LLM services are discovered through Kubernetes service metadata and selected by
Cortex based on health, role, capabilities, modalities, and runtime preferences.

### Memory

The memory service stores users, sessions, chat messages, claims, notes,
summaries, and prompt context.

Cortex uses memory to resolve sessions, persist chat history, retrieve recent
context, and support future long-running task workflows.

### Primer

Primer is the shared model runtime facade used by Cortex and LLM services.

It handles backend loading, model loading, message adapters, token counting, and
text, JSON, and streaming generation.

### Tool

The tool runtime role is planned for non-LLM executable tools.

This may include search, inspection, retrieval, system operations, or other
specialized backend capabilities.

### Federation

The federation runtime role is planned for trusted inter-cluster communication.

Federation is not currently treated as a working runtime path. It is a future
boundary for remote cluster discovery, delegation, and policy-controlled task
exchange.

## Runtime protocol

Orin uses canonical runtime messages internally instead of treating provider-
specific APIs as the internal contract.

Runtime messages are composed of typed content parts such as text, image, audio,
video, JSON, and binary data.

Executable work is represented as WorkPackets. A WorkPacket contains a canonical
task, routing hints, execution disposition, and metadata.

The long-term goal is that LLM work, memory work, tool work, and deferred Cortex
work all use the same WorkPacket-based execution model.

## Kubernetes model

Orin is designed to run on Kubernetes, with k3s as the current practical target.

The bootstrap process deploys the minimum working runtime:

- memory
- cortex

Additional runtime services, such as LLM pods, are deployed later through Cortex
deployment operations.

Cortex discovers backend services through Kubernetes service labels and
annotations. Runtime metadata such as loaded model, capabilities, modalities, and
context window can be patched onto services and used for routing.

## Deployment model

The current deployment model separates initial host bootstrap from ongoing pod
deployment.

`install-orin.sh` is responsible for setting up the initial cluster host,
namespace, RBAC, configuration, secrets, memory service, and Cortex service.

Cortex deployment operations are responsible for adding nodes, installing k3s
agents, labeling nodes, deploying runtime pods, and patching discovery metadata.

## Terminal client

`terminal-chat.py` is a lightweight terminal client for interacting with Cortex.

It sends canonical runtime messages to Cortex through the `/work` endpoint and
supports both chat-style interaction and slash-command operation.

Command output and assistant chat output are separated by response metadata so
the terminal can display operational output separately from conversation output.

## Status

Orin is under active development.

The current codebase is experimental and focused on local Kubernetes edge-AI
runtime development.

Current near-term goals include:

- making WorkPacket the single execution unit across Cortex, LLM, memory, tool,
  and deferred work
- unifying deferred task execution
- finishing the standard-vs-Jetson image split
- improving service discovery metadata patching
- adding the first real tool backend
- persisting deployment inventory
- cleaning up old gateway/orchestrator terminology

## License

Orin source code is licensed under the Apache License, Version 2.0.

See `LICENSE` for the full license text.

Model weights, third-party containers, runtime libraries, and external services
may be governed by their own licenses. See `THIRD_PARTY_LICENSES.md`.
