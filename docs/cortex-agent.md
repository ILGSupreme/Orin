# Unified Cortex agent runtime

Cortex owns persistent goals and their feedback loops. Both natural `/chat` and
`/terminal_chat` requests use one turn interpreter and one task execution runtime.
There is no user-facing chat/agent mode or separate background chat pipeline.
Explicit terminal slash commands remain direct controls. The implementation plan
is [unified-agent-action-plan.md](unified-agent-action-plan.md).

## A natural turn

> Hello, explain this image, get the latest news about X, and summarize this document.

The interpreter can return a conversational reply and several task specifications
in the same result. Each task has an outcome, completion criteria, relevant user
context, references to supplied input parts, and dependencies. Straightforward
conversation can finish in this one inference call. Requested work is persisted
and runs through the same runtime without asking the user to choose a mode.

Independent tasks receive round-robin execution steps. They are not executed in
parallel within a goal in this version. A task that needs clarification blocks
its dependents while independent work continues. Each decision sees only that
task's context, recent observations, advertised capabilities and explicit
prerequisite results. Work actions receive only selected original input parts.
The interpreter sees bounded previews and references, not image bytes.

Tasks that need current information must execute a real retrieval tool. Provider
access and LLM reasoning are not search capabilities. Missing capabilities produce
an explicit limitation; the runtime must not fabricate current news or tool effects.

## Execution and responsibility feedback

For each ready task, Cortex chooses an action, checkpoints it before execution,
collects an observation, then decides whether to continue, finish, or ask a
question. Failed actions are observations. Completion must cite successful
observation IDs from the current cycle or completed dependencies. These checks
validate evidence references; semantic completion remains model-assessed.

The responsibility loop periodically queries Memory for due goals. It does not
keep an LLM generating continuously. Supported wake-ups are:

- A new natural turn that supplies clarification or corrective feedback.
- An explicit future UTC epoch start time.
- An interval of at least 60 seconds for recurring work.
- A matching event delivered through the local goal control interface.
- Restart recovery of persisted ready work after any prior lease expires.

Schedules/events must be explicitly requested by the user, as interpreted by the
model. Triggered tasks must be independent of task dependency graphs. Calendar
rules, delayed event subscriptions and arbitrary event-source connectors are not
implemented. Interval/event monitoring returns to waiting after each assessed
cycle, including a cycle that reports an explicit limitation. Limits or invalid
execution instead pause work for user/operator input.

## Durable state and recovery

Memory owns a new `agent_goals` SQLite table. It stores user/session ownership,
tasks and dependencies, source input, privacy, observations, pending work IDs,
results, bounded notices, budgets, revisions and scheduling state. The operations
`agent_goals_query`, `agent_goal_write` and `agent_goal_claim` use the existing
WorkPacket boundary. Cortex pins these requests to its authoritative Memory URL,
using `inventory_memory_base_url` or the namespace's Memory service address.

Writes use compare-and-swap revisions. Atomic execution claims use expiring leases,
so two controllers cannot execute the same live claim. Leases last one execution
slice plus 30 seconds; stale owners cannot save over a newer claim. Source input
and state are currently stored together, so large attachments increase checkpoint
size. Artifact-backed inputs are a future optimization.

After a crash, work interrupted during an action is paused for reconciliation,
not replayed automatically. The recorded pending work ID helps inspect an accepted
backend job. A clarification survives restarts and can resume the same task with
user feedback. Corrective feedback can also reopen completed work and invalidate
its dependent outcomes, requiring fresh evidence. Cancellation marks the durable goal cancelled so the watcher will
not restart it. Native inference or backend work already accepted may continue;
cancellation does not undo effects. Non-streaming Primer generation keeps its
serialization lock until underlying native inference really finishes, even if its
caller has been cancelled.

Client `metadata.turn_id` is an optional idempotency key for delegated turns. Retry
with the same user, session and turn ID to reuse the accepted goal; use a new ID
for new work. Ordinary conversational replies without delegated goals are not
stored as goal idempotency records.

The latest results and up to 100 changed-result notices are durable and exposed to
the local client. Repeated identical results do not produce another notice. Delivery
is by polling or conversational status/result requests. The terminal client polls
in the background and displays results/questions automatically, deduplicating
notice IDs and showing the latest result per goal on reconnect. Set
`ORIN_SESSION_ID` to resume a known session and `ORIN_PRIVACY` to override the
server's default inference privacy for terminal turns. External push, email and
other notification connectors are separate capabilities. JobManager holds bounded
transient execution records, not the source of truth for responsibilities.

## Replaceable inference

`InferenceGateway` is the shared interface for turn interpretation, task decisions
and work inference. Configured bindings select local Primer, discovered cluster
LLMs, or an OpenAI Responses provider. Immediate replies come from turn
interpretation and task answers come from completion decisions.

Local engine imports, hardware profiling and initialization are lazy. Provider-only
Cortex does not need a loaded local model or import torch, llama.cpp or vLLM to
serve natural conversation. Cortex readiness reports configured inference route
availability rather than requiring the local Primer model. Routing, Kubernetes
and Memory remain normal Cortex infrastructure dependencies.

Example configuration fragment for provider-only reasoning:

```json
{
  "inference": {
    "allow_external": true,
    "default_privacy": "external_allowed",
    "backends": {
      "provider": {
        "kind": "openai",
        "model": "<model-supported-by-your-account>",
        "api_key_env": "OPENAI_API_KEY",
        "context_tokens": 32768,
        "modalities": ["text", "image"]
      }
    },
    "bindings": {
      "interpret": ["provider"],
      "decide": ["provider"],
      "work": ["provider"]
    }
  }
}
```

Choose the model, context allowance and modalities for the actual backend. The
model name is intentionally not hardcoded. Set the key through the configured
environment variable, e.g. a Kubernetes Secret reference in the Cortex deployment;
do not put the key in the configuration file or task data. The adapter uses the
Responses endpoint, strict JSON-schema output for structured contracts, bounded
output, `store: false`, no followed redirects and sanitized provider failures.
Refusals, incomplete responses and invalid structured output are not successes.
See the [official structured output documentation](https://developers.openai.com/api/docs/guides/structured-outputs).

For a hybrid setup, keep a `primer` backend for interpretation/decisions and a
`routed` or provider backend for work. Binding lists are explicit ordered fallback
choices. Fallback never bypasses privacy policy. Routed work remains a canonical
LLM WorkPacket; Planner remains responsible for backend selection.

External inference is disabled by default. Both `inference.allow_external` and
per-turn/goal `privacy="external_allowed"` are required. Otherwise a provider is
skipped before any transport call. The trusted caller can set `metadata.privacy`;
model outputs cannot grant external access. Goals retain their original policy.
Later provider-enabled turns exclude local-only goal summaries and private or
unlabelled conversation history. A reply containing a private goal result remains
labelled private so a subsequent turn cannot silently export it.

## Actions and extensibility

Built-in actions are read-only local cluster snapshot/node/workload inspection,
LLM analysis, summarization and input description. Inspection uses an explicit
backend-free command context, permission checks and no arbitrary slash arguments.
Image work additionally requires a configured image-capable inference route. The
provider adapter supports text and image input; arbitrary binary/document file
uploads, audio and video are not implemented. Documents supplied as text parts can
be referenced by tasks.

Register actual read-only Tool backends through Cortex configuration:

```json
{
  "agent_tools": [
    {
      "name": "news_search",
      "operation": "search",
      "description": "Retrieve current news with source URLs and dates",
      "capability": "search",
      "input_key": "query",
      "read_only": true
    }
  ]
}
```

This does not create a search service. A healthy discovered `tool` backend must
advertise the required capability, accept that WorkPacket operation/input contract,
and return usable RuntimeMessage content. Otherwise it is not advertised to the
model. No deployment, arbitrary shell, model administration or write tools are
granted to this runtime. Adding an action requires a trusted implementation and
configuration; model output cannot register one.

## Local interfaces

`/chat` and `/terminal_chat` preserve the existing request shape. Replies contain
any immediate answer plus an acknowledgement of pending work, with `goal_id` and
transient `job_id` in metadata. Stream responses return the assembled reply and
correlation headers; incremental provider token streaming is not implemented.

`GET /goals?user_id=...&session_id=...` returns durable scoped status, task results
and notices. It excludes stored source messages and execution lease identity.
`POST /goals/{goal_id}/control` accepts:

```json
{
  "user_id": "user-id",
  "session_id": "session-id",
  "action": "resume",
  "task_id": "t1",
  "feedback": "Use node-a",
  "event_key": null
}
```

Actions are `resume`, `cancel` and `event`. For an event, supply its registered
`event_key` and relevant data as `feedback`. Controls verify user/session scope;
resuming an actively executing goal is rejected. `/cancel_job <job_id>` delegates
to durable cancellation for goal jobs. These are local Cortex interfaces;
Federation's direct `/work` ingress remains LLM-only and gains no goal controls,
Memory operations or agent permissions. Existing local identity/trust assumptions
remain unchanged; caller-provided user IDs are not a new authentication mechanism.

## Limits and validation

Defaults: six decision steps per task/cycle, 180-second execution slices,
32,768 allocated tokens per goal, four active goal runs and five-second wake-up
polling. All actions and decisions pass bounded structured contracts. Repeating
an identical action/input in a cycle pauses that task. The allocation budget is
persistent across resumes and monitoring cycles; changing it requires operator
configuration. No automatic model-output repair retries are added.

Estimates include prompt, schema where applicable, configured output and safety
allowances. Provider usage can increase the recorded allocation after a call.
These estimates are not an exact billing cap; image token estimates are approximate.
Context limits are enforced against configured capabilities, with local token
counting when available. Small model decision quality and decomposition completeness
still require lab evaluation. Timeouts are cooperative for synchronous/native code.

Automated validation uses the real Memory handlers/SQLite and actual gateway,
Harness, protocol, action permission and JobManager code, with model/tool/HTTP
boundaries doubled. Run:

```bash
PYTHONPATH=src python -m unittest discover -s src/test -p 'test_*.py'
python -m compileall -q src
```

The automated suite has 85 passing tests on Python 3.10 and 3.12, including API lifespan startup and chat
with provider-only inference, a real SQLite-backed Memory service behind a mock
HTTP transport, and terminal notice deduplication. Static checks include compilation,
whitespace validation and Ruff correctness checks on the changed runtime modules.
Live provider credentials, real model decisions, Kubernetes workload execution,
interactive terminal redraw behaviour and end-to-end external notification delivery
have not been tested. Keep the PR draft
until lab validation is complete.

Inference, goal slices and pipeline stages use the `async-timeout` compatibility
library because current images run Python 3.10. Inference failures return a
sanitized HTTP 503; operator logs include backend, role and exception type without
provider response bodies or submitted data.

GGUF interpretation and decision requests compile the validated response schema
into a llama.cpp grammar before native generation. Runtime validation still checks
the generated result; grammar constraints do not establish semantic correctness.
The grammar projection omits string/array repetition bounds to avoid native
parser rule explosion. Original length/item bounds remain in Pydantic validation;
output token limits still apply. CI separately compiles both agent grammars with
the actual CPU native parser without loading model weights.
Other local engines retain prompt guidance and result validation. Read-only
inspection actions accept an empty input or an exact redundant echo of their own
action name; arbitrary arguments remain prohibited.
