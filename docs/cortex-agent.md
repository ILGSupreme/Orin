# Cortex bounded agent runs

Cortex supports a local `agent` pipeline alongside the existing six-stage `chat`
workflow. It selects one action, executes it, records the observation, and uses
those observations to choose the next action or finish. RouterService and
WorkPacket remain the inference execution path; Planner still selects backends.

## Starting and inspecting a run

In `/chat`, explicitly ask to use agent mode, for example:

> Use agent mode to inspect the local cluster step by step and report what you
> can establish about node and workload readiness.

The lightweight interpreter selects `start_pipeline` with `pipeline="agent"`.
Ordinary questions retain the immediate response path. Other detailed work can
still use `pipeline="chat"`. This choice currently depends on the interpreter;
there is no new direct agent HTTP endpoint.

Harness returns a background job ID. Ask for its status or result in the same
session, or inspect the existing local `GET /jobs/{job_id}` endpoint. Explicit
terminal command `/cancel_job <job_id>` cancels the selected local job. Shutdown
also cancels tracked local jobs. Cancellation is represented as a failed
WorkResult with `metadata.cancelled=true`, preserving the existing status contract.
Cancellation does not cancel backend work already accepted, or automatically
cancel independent child jobs created by older pipelines.

## Decisions and permitted actions

`agent_types.py` defines JSON-serializable state, limits, observations, and a
discriminated decision union. Unknown decisions, unknown actions, and extra
fields fail validation. Invalid decisions end the run; there are no automatic
model-output repair retries in this version.

| Decision | Behaviour |
| --- | --- |
| `execute_action` | Execute one permitted action and record success or failure. |
| `finish` | Return a complete or explicitly partial answer. A successful finish must reference successful observation step IDs. |
| `ask_user` | Return a clarification question and end this run with outcome `awaiting_user`. |

| Action | Input | Execution |
| --- | --- | --- |
| `cluster_snapshot` | No arguments | Read discovered backend state and active jobs. |
| `list_nodes` | No arguments | Read configured node inventory and its cluster status. |
| `list_pods` | No arguments | Read workloads in the configured namespace. |
| `analyze`, `summarize` | `text`, at most 2,000 characters | Route LLM work with recent observations. These actions do not retrieve live facts. |

Inspection actions use an explicit backend-free CommandContext and recheck
command permissions. They do not inherit the terminal's selected backend or
folder. No deployment, mutation, shell execution, network search, arbitrary
slash commands, or backend administration is exposed to the agent.

The direct `/work` ingress continues to accept only LLM work. Federation does
not gain an agent entry point or any new permissions. Observation content is
presented as untrusted data, and code controls the available actions.

## State and limits

State records the goal, completion criteria (initially the original goal), run
outcome, step count, token allocation, pending action/work ID, and observations.
Every action receives a unique work ID. Progress snapshots live in the job's
`agent_state` payload; completed results also contain the final state.

Defaults are six decision steps, 180 seconds of cooperative execution time,
16,384 estimated tokens allocated across model calls, and one execution of an
identical action/input pair. Finish and clarification decisions also consume a
step. The token allocation includes prompt estimates, operation-specific output
allowances, and Primer's safety allowance; it is not measured usage accounting.
Limits are configured through `AgentLimits` in code, not through user/model fields.

Each observation retains at most 2,000 characters. Prompts use the latest three
observations, at most 600 characters each. Decision prompts are checked against
the local model context; routed LLM work selects a backend with the required
context capacity. Full run snapshots retain all bounded observations.
Successful evidence references are validated, but semantic goal satisfaction is
still a model assessment, not an independent proof or domain-specific verifier.

Step and runtime exhaustion return a failed WorkResult with outcome `exhausted`
and the observations available so far. A model-declared partial result is a
completed WorkResult with outcome `partial`, distinct from a completed goal.
Failed actions become observations so a subsequent decision can choose another
action or explain the limitation. Identical action repetition ends the run.

JobManager now applies each stage timeout to both its runner and polling,
including a single stalled await. Synchronous code and underlying inference
threads must still cooperate with cancellation; Python task cancellation cannot
preempt them or roll back completed effects.

## Existing workflow fixes

The chat execution stage now collates batch results before final synthesis.
Both ingress interpretation models accept `get_job_result`, and their prompts
advertise it. Shaped task packets use unique IDs. Completed-job prompt summaries
include compact result text instead of the full payload/run state.

## Remaining work

Run snapshots are in memory. Durable Memory checkpoints, restart recovery,
resuming a clarification in the same run, a broader typed Tool backend, and
explicit targets for generic backend actions remain future work. A clarification
currently ends execution; further work requires a new request/run. Validate small
model decision quality and live cluster operation before deploying this change.

## Validation

Run `PYTHONPATH=src python -m unittest discover -s src/test -p 'test_*.py'` and
`python -m compileall -q src`. Agent tests use deterministic model/transport
doubles; Harness tests replace heavyweight inference/hardware imports while
exercising real Harness, command-router, JobManager, and protocol code. No live
GPU model or Kubernetes cluster is required for these checks.
