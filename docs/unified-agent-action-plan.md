# Unified Cortex agent action plan

Goal: one natural conversational interface to a persistent servitor. Cortex owns
goals, permissions, execution and feedback; inference and action providers are
replaceable. A greeting, explanation, current-information request and delegated
work can coexist in one turn without selecting a mode.

Implementation order:

1. Define shared turn/task/goal contracts, dependencies, observations, privacy,
   budgets, clarification, recurrence and event wake-ups.
2. Introduce an inference gateway with operation-role bindings to local Primer,
   routed cluster LLMs and an OpenAI Responses adapter. Keep model/credentials
   configured externally; permit external context only through explicit policy.
3. Make local engines/profiling lazy so provider-only Cortex does not import or
   initialize GPU libraries. Readiness must depend on selected inference routes.
4. Store versioned goals in Memory using dedicated operations and SQLite records,
   compare-and-swap updates and expiring execution leases.
5. Replace chat/agent mode selection with a unified turn interpreter. Separate
   immediate replies from requested outcomes, validate task dependencies, and
   scope each task's context and attachment references.
6. Execute independent ready tasks with a bounded observe/decide/act/verify loop;
   preserve partial results and block only dependent work when input is missing.
7. Add persistent clarification, feedback, cancellation, due-time/event wake-up
   and restart recovery. Never replay an uncertain in-flight action automatically.
8. Integrate both natural chat ingress surfaces, progress/results and lifecycle
   shutdown with the shared runtime. Keep explicit terminal commands and
   Federation's LLM-only ingress boundary.
9. Test mixed requests, dependency validation, unavailable capabilities, scoped
   inputs, real SQLite recovery/leases, clarification, recurrence/events, privacy,
   provider-only operation and provider API edge cases using transport doubles.
10. Rewrite PR #1 and its documentation around this architecture; retain useful
    cancellation, timeout, result-retrieval and correlation fixes. Leave it draft
    pending live model and lab validation.

Current-information retrieval requires a registered real tool. Model reasoning
alone must never be advertised as current news retrieval. Event/schedule handling
wakes bounded runs; it does not keep an LLM generating continuously. Notifications
are durable results available to the local client; external delivery connectors
are separate capabilities.

## Implementation status

Steps 1-8 are implemented in the reworked PR. Step 9 has 76 passing automated
tests, including provider-only API startup, real SQLite persistence/leases,
restart and clarification recovery, feedback invalidation, privacy boundaries,
scoped inputs, mixed requests and terminal notices. Step 10 updates the same
existing draft PR and the architecture documentation.

Lab acceptance remains: real small-model decomposition and decisions, live
provider configuration, real retrieval/tools, Kubernetes operation and terminal
redraw under interactive input. These checks require the future lab environment;
none of them is claimed by the transport/model-double tests.
