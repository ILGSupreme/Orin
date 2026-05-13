# Contributing to Orin

Thank you for your interest in contributing to Orin.

Orin is a local edge-AI cluster runtime for Kubernetes. The project is currently
in early development, so architecture and APIs may change.

## Development principles

Contributions should follow these principles:

- keep Cortex responsible for ingress, routing, discovery, command handling, and
  orchestration
- keep backend runtimes focused on executing WorkPackets
- prefer canonical runtime messages over provider-specific request formats
- avoid hardcoding backend-specific behavior into Cortex when it belongs in an
  adapter or runtime
- keep Kubernetes discovery metadata explicit and patchable
- keep Jetson-specific behavior separate from standard amd64/arm64 behavior
- avoid introducing hidden global state where explicit runtime state is better
- prefer simple, testable code over premature abstraction

## Project structure

```text
src/common/       Shared protocol, runtime, adapters, model helpers
src/cortex/       Cortex app, routing, discovery, deployment, command interface
src/llm/          Primer-backed LLM backend
src/memory/       Memory backend and persistence
src/tool/         Planned tool backend
src/federation/   Planned federation backend
documentation/   Architecture and operations docs
```

## Commit style

Use clear, direct commit messages.

Examples:

```text
Add WorkPacket polling timeout
Fix service annotation patching
Refactor LLM runtime naming
Add Cortex attach endpoint
```

## Pull requests

A good pull request should include:

- a clear description of the change
- why the change is needed
- affected components
- test notes
- migration notes, if behavior changes

## Testing

Before submitting a change, run the relevant local checks and include the results
in the pull request.

Suggested checks:

```bash
python -m compileall src
```

Add project-specific tests here as the test suite becomes formalized.

## Licensing

By contributing to Orin, you agree that your contribution will be licensed under
the Apache License, Version 2.0.

Do not contribute code that you do not have the right to license.

Do not copy code from projects with incompatible licenses.

## Third-party dependencies

When adding dependencies, include:

- package name
- license
- reason for use
- whether it is required at runtime or only for development
- any platform-specific constraints, especially for Jetson

Update THIRD_PARTY_LICENSES.md when relevant.

## Models and weights

Do not commit model weights to the repository.

Do not assume that a model license is compatible with Orin's source-code license.
Model usage, redistribution, and commercial use must be checked separately.
