# Local coordinator pilot

A user starts one local Codex coordinator. It delegates investigation,
implementation and verification, then hands a draft PR to the human for CI and
lab evaluation. This is development tooling, independent of the Orin runtime.

## Prerequisites

- Linux, Python 3.10+, git, GitHub CLI (`gh`), and an authenticated current Codex CLI.
- Codex must support project `.codex/agents/*.toml`, `agents.enabled`,
  `agents.max_concurrent_threads_per_session`, `exec --output-schema` and
  `--sandbox workspace-write`. Older CLI versions may need updating.
- Authenticate locally using `codex login` and `gh auth login`; configure git's
  author identity and HTTPS push credentials (`gh auth setup-git`).
- Use an activated test virtual environment with `requirements-test.txt`
  installed. Subprocesses inherit PATH. No dependency installation is automatic.
- Review and trust the checkout's Codex configuration locally before the first
  run. Project configuration may be ignored for untrusted repositories. If agent
  roles are unavailable, the coordinator must return blocked.

Official runtime references:
- https://learn.chatgpt.com/docs/agent-configuration/subagents
- https://learn.chatgpt.com/docs/non-interactive-mode

This pilot does not pin a model: roles inherit the user's selected model and
account configuration. Local execution still uses the configured inference
provider and its usage limits; it does not run on Orin's Qwen model by default.

## Start

Run the controller from a checkout containing these files. Keep run directories
outside the repository, preferably on `/data` on the gateway. Each run makes its
own clone and branch, leaving your working checkout alone.

Write a task to `/data/orin-tasks/task.md`, specifying the desired outcome and
scope. For example: “Investigate and fix cancellation bugs in src/llm. Add
regression tests and prepare human lab test instructions. Do not deploy.”
For an audit without edits, ask for a findings document in the repository.

```bash
python tools/local-agent/run.py \
  --task /data/orin-tasks/task.md \
  --checks tools/local-agent/checks.example.json \
  --run /data/orin-agent-runs/llm-cancellation \
  --publish
```

Omit `--publish` to keep the entire result local. The task starts immediately;
`--publish` authorizes a push and draft PR after local checks. No image build,
cluster mutation, merge or human acceptance is performed by the controller.
For testing this workflow before it merges, add `--base local-agent-workflow`;
that base must already exist remotely and the resulting task PR targets it.

Check commands are JSON arrays of argument arrays, executed without a shell.
Copy the example and tailor it to your task. These checks are captured in state
before the agent runs. The example is a CPU baseline; it does not replace the
hosted Python matrix, native grammar smoke test or relevant specialist tests.

The default agent invocation limit is 45 minutes, with 15 minutes per controller
check. Override with `--minutes` and `--check-minutes`. Agent concurrency is capped
at three; instructions permit two repair rounds. These are not billing caps.

## Feedback and recovery

The run directory contains `state.json`, the isolated `checkout`, and one
directory per attempt with the prompt, JSONL agent log, result and check logs.
States include running, blocked, no_changes, local_ready and awaiting_human.
Logs stay local and can contain sensitive data; review before sharing.

Put your CI or lab findings in a feedback file, then resume the same checkout:

```bash
python tools/local-agent/run.py \
  --run /data/orin-agent-runs/llm-cancellation \
  --feedback /data/orin-tasks/feedback.md \
  --publish
```

Each attempt starts a new coordinator conversation with the original task,
current checkout and supplied feedback. Include relevant previous findings in
feedback; this is not automatic restoration of the previous model conversation.
Checks rerun, a new candidate commit is recorded, and the existing PR is updated
and returned to draft if necessary. No force-push is used. Remote branch changes
cause a push failure requiring reconciliation. Closed/merged PRs require a new run.
One process may own a run at a time. A timeout/interruption stops the subprocess
group and records blocked; partial files remain for inspection and recovery.

CI failures do not autonomously trigger repairs in this pilot. Supply feedback
explicitly. GitHub workflows already run when the PR is opened or updated.

## Human acceptance

1. Wait for CI on the candidate commit.
2. Build the candidate using the existing lab build procedure. Record its commit
   and actual image digest. The controller never invents an image identifier.
3. Deploy and execute the PR's lab instructions yourself.
4. Record the tested commit, image digest, configuration, evidence and pass/fail.
5. On failure, provide feedback. On success, review and merge yourself.

Any change after testing needs fresh acceptance. `awaiting_human` means a draft
PR exists; it does not mean CI or lab tests passed. Never infer acceptance from
the coordinator's confidence or a passing unit test.

## Boundaries and limitations

Use a dedicated unprivileged development account or isolated development
environment without cluster write credentials, SSH deployment keys or Docker
socket access. Agent roles are instructions, not separate OS identities. All
subagents inherit the parent's permissions and configured MCP servers. The CLI
workspace sandbox limits writes but is not a complete credential isolation
boundary. Do not give this agent access to live deployment tools.

The controller runs repository checks on the host and publishes using your git/gh
identity. It is for your trusted, manually selected tasks, not arbitrary public
issue triggers. Keep controller code and run state outside the agent checkout.
Test success alone does not prove meaningful coverage; independent review and
human acceptance remain necessary.

No always-on daemon, automatic CI repair, image builder, token-budget service or
automatic lab acceptance is included. This pilot supplies the local task-to-PR
loop first. The held confirmation-loop issue is not started by installing it.

## Validate this tooling

```bash
python -m unittest discover -v -s tools/local-agent/tests
```

Tests cover blocking, check failure, draft-only publishing, same-PR feedback,
local-only mode, result validation and process timeout. GitHub/Codex calls in
controller tests are simulated; a real local authenticated run remains required.


## Specialist completion gate

Every new attempt now requires Codex `SubagentStart` and `SubagentStop` hooks.
The controller sets `ORIN_AGENT_EVIDENCE` for the Codex invocation. The hook
records only lifecycle identities and final reports in `attempt-N/specialists.jsonl`.
It does not copy conversation transcripts. The controller verifies the parent
thread against that attempt's `thread.started` event, checkout, distinct agent
identities, all three named roles, and structured passing final reports.
The final verifier must start after other workers finish. Its checkout fingerprint
must still match after coordinator exit and controller checks. Missing, malformed,
untrusted/skipped hooks or reports leave the run blocked before commit/push.

Each specialist must return plain JSON with verdict (`pass` or `blocked`),
summary (nonempty string), and evidence (nonempty string list). Verified records
are saved in `verified-specialists.json` locally; only a short verification
statement goes into the PR. A blocked or malformed report stops the attempt;
use explicit feedback in a new attempt to repair it.

Before your first attempt, add `--prepare` to your new-run command. This creates
and records the isolated checkout without invoking Codex. Then open Codex inside
that checkout, review/trust the project and use `/hooks` to review the two hooks.
Exit Codex, and repeat the controller command without `--prepare`. A prepared
run does not require a feedback file. Hook trust is never bypassed automatically.
If your CLI does not support these hooks, stop and report its diagnostic output.
The hook behavior still needs live validation on the installed CLI version.

Official hook contract: https://learn.chatgpt.com/docs/hooks

The evidence gate detects absent/stale reports and ordinary workflow mistakes.
It is not cryptographic attestation or an adversarial security boundary: hooks,
agent processes and the controller share the operator's account. Do not expose
this workflow to untrusted tasks. A captured passing report proves what a
specialist reported, not that every technical claim in it is correct.

Upgrade existing pilots using a fresh prepared run so the checkout includes the
new hooks and role instructions. Keep old attempts as historical evidence.
