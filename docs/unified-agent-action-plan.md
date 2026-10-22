# Unified Cortex agent action plan

Goal: one natural interaction flow for conversation, information queries and
delegated work. Cortex owns goals, execution and feedback; inference and action
implementations remain replaceable.

Documentation review: October 22, 2026. The implementation is present on
`main`. Merge status and live acceptance are separate facts.

## Implemented foundation

1. Shared turn/task/goal contracts, dependencies, observations, privacy,
   budgets, clarification and schedule/event wake-ups.
2. An inference gateway with explicit interpretation, decision and work bindings
   to local Primer, routed LLMs and an OpenAI Responses adapter.
3. Lazy local engine initialization for provider-only reasoning.
4. Memory-backed goals with revisions, expiring claims and recovery state.
5. A unified natural-turn interpreter for chat and terminal ingress.
6. Bounded task execution with observations, evidence references and dependency
   results. Ready tasks advance round-robin within a goal.
7. Persisted clarification, corrective feedback, cancellation and wake-ups.
   Uncertain in-flight actions require reconciliation rather than blind replay.
8. Goal polling and terminal notices, with explicit slash commands kept separate
   and Federation's direct Cortex WorkPacket ingress restricted to LLM work.

See [the runtime](cortex-agent.md) for exact configuration and limitations.
Source implementation does not prove availability of the required services,
permissions or inference routes in a deployment.

## Automated coverage

The suite covers mixed turns, dependency validation, unavailable capabilities,
scoped inputs, SQLite recovery and leases, clarification, feedback invalidation,
recurrence/events, privacy, provider transport behavior, evidence references and
terminal notices. Model/tool/provider boundaries use doubles. Kubernetes discovery
also uses a local HTTP server; native grammar compilation is a separate CPU job.

Use [CI](ci.md) and the exact commit's run for commands and results. Fixed test
counts and historical draft-PR instructions are not current acceptance evidence.

## Acceptance order

1. Record the deployed revision, image digests, dependencies, model settings and
   enabled inference bindings. Confirm actual connections and permissions.
2. Prove a read-only node/workload request through a real action, stored
   observation and persisted outcome. Exercise unavailable and denied actions.
3. Repeat representative conversation, inspection, mixed and dependent requests
   with the actual small model. Evaluate correct decisions and truthful limits.
4. Verify clarification, feedback, cancellation, restart/lease recovery and
   inventory persistence against real Memory without replaying uncertain work.
5. Validate provider routes and privacy policy when credentials are available.
   Test real retrieval only with an implemented, authorized service.
6. Verify terminal results/questions during interactive input and reconnect.
7. Revalidate Federation submission, polling and sanitization against current
   Cortex. Harden administrative and polling authorization before public use.
8. Add further tools and then automatic federated routing or independent remote
   delegation when the local execution/feedback loop is reliable.

For every run, retain request and turn/goal/job/action identifiers, observations,
completion evidence and failures. Schema compliance proves output structure;
evidence references prove linkage; neither proves a sound completion decision.

Current-information requests require actual retrieval. Schedules/events wake
bounded runs rather than keeping inference continuously active. External
notifications need a separate delivery capability.
