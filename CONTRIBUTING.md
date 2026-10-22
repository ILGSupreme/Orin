# Contributing to Orin

Orin is an experimental distributed assistance platform. Kubernetes/k3s, Jetson
and standard Linux hosts are the current deployment targets. Start with the
[README](README.md) and [Cortex runtime](docs/cortex-agent.md).

## Engineering principles

- Keep Cortex responsible for conversation, goals, execution and feedback.
- Keep inference backends replaceable and use canonical runtime messages and
  WorkPackets at service boundaries.
- Keep capability discovery, authorization and execution distinct. A discovered
  backend or a model's proposed action grants no permission.
- Prefer defined service operations. Introduce independent remote agents only
  when planning, local authority or ongoing responsibility adds value.
- Preserve the Federation boundary: internal service URLs, Memory, credentials,
  deployment controls and local agent privileges are not public capabilities.
- Keep Jetson-specific images and native dependencies explicit.
- Inspect code and configuration, reproduce failures where practical, and prefer
  targeted changes over speculative infrastructure.
- Require execution observations for claimed outcomes. Valid JSON or a passing
  unit test does not prove correct model behavior or successful deployment.

## Project structure

| Path | Purpose |
| --- | --- |
| `src/common/` | Protocols, runtime types, Primer, model adapters and jobs |
| `src/cortex/` | Assistant/agent runtime, inference gateway, commands, discovery, routing and deployment |
| `src/llm/` | Primer-backed inference service |
| `src/memory/` | Conversation, goals, node inventory and Federation persistence |
| `src/federation/` | HTTP Federation MVP: membership, signatures, policy and work exchange |
| `src/test/` | Deterministic tests and separate live/native smoke scripts |
| `docs/` | Focused runtime, CI, storage and design documentation |
| `tools/lab-mcp/` | Lab access bridge |
| `tools/local-agent/` | Local development and validation tooling |

The Tool role exists in protocols and configuration; no standalone `src/tool/`
implementation is currently present. Built-in inspection can live inside Cortex.

## Changes and validation

Use clear commit messages describing the resulting behavior. Pull requests
should explain the problem, final change, relevant checks, remaining limitations
and any upgrade impact.

Follow [CI and local test instructions](docs/ci.md), including the workflow's
focused Ruff scope. Use Python 3.10 and 3.12 for the ordinary CPU checks.
Run checks appropriate to the change; documentation-only edits need link,
example and formatting checks rather than model or hardware tests.

State which boundaries tests replace with doubles. Keep live model/provider,
native runtime and cluster validation separate. Never expose persistent lab
runners or credentials to unreviewed public pull-request code.

For operational changes, identify effects on configuration, persistent data and
running services. Record tested commits, image digests, model/runtime settings,
dependency versions and correlated goal/job/action evidence. Do not merge or
deploy without authorization within the agreed scope.

## Persistence and upgrades

Memory owns durable goals and worker inventory. JobManager records are
transient. Consult [storage upgrades](docs/memory-storage-upgrade.md) and
[node inventory](docs/node-inventory-api.md) when changing persistence.
Do not infer SSH credentials from Kubernetes objects or replay an uncertain
in-flight action automatically after recovery.

## Documentation

Update documentation with behavior changes. Distinguish implemented code,
automated coverage, dated lab observations and intended capabilities. The current
architecture overview is
[architecture and operations](orin-cluster-current-architecture-and-operations.md).
The Federation plan contains proposals as well as implementation status; the
older TeX reference is explicitly historical.

## Licensing and dependencies

Contributions are licensed under Apache License 2.0. Only contribute code you
have the right to license, and avoid incompatible third-party code.

For dependencies, record the package, license, reason, runtime/development scope
and platform constraints. Update [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)
when relevant. Do not commit model weights or assume their licenses match
Orin's source license.
