# Local coordinator smoke test

This walkthrough exercises `tools/local-agent/run.py`, the development controller
that runs a local Codex coordinator in an isolated checkout. It does not exercise
Orin runtime behavior. The instructions below are for a human operator; writing
this document does not authorize running the controller, publishing, building an
image, or accessing a cluster.

## Acceptance criteria

For this documentation smoke task:

- Only `docs/local-agent-smoke-test.md` changes.
- The coordinator delegates investigation of `tools/local-agent` to an
  investigator, bounded document edits to an implementer, and an independent
  final accuracy review to a verifier.
- The document explains task input, checkout isolation, local checks, optional
  draft publication, feedback continuation, and human acceptance.
- Both required check commands below pass locally, and the result records actual
  evidence and review limitations without claiming CI or lab acceptance.

The coordinator must follow repository instructions, derive explicit acceptance
criteria, and keep work within the approved task. An inspection request alone
does not authorize fixes; an audit should request a repository findings document.
Agents share one checkout: assign disjoint files and serialize overlapping edits.
Use at most three concurrent agents, no recursive delegation, and stop workers
before final review. Resolve verified blockers within scope in at most two repair
rounds. Missing specialist delegation or dependencies means `blocked`, not a
substitute claim of multi-agent review. Do not weaken checks or change controller,
agent configuration, or tests without explicit task authorization.

## Prepare task and environment

Use Linux, Python 3.10+, git, and an authenticated current Codex CLI. Codex must
support project `.codex/agents/*.toml`, `agents.enabled`,
`agents.max_concurrent_threads_per_session`, `exec --output-schema`, and
`--sandbox workspace-write`. Review and trust the project configuration locally;
verify that investigator, implementer, and verifier roles are available. Roles
inherit the selected model and permissions; they are not separate OS identities.
Use a development environment without deployment credentials or Docker socket
access. Inherited MCP configuration does not authorize using external services.

Activate a test virtual environment with the repository's
`requirements-test.txt` installed, for example using `python -m venv` and
`python -m pip install -r requirements-test.txt` inside that environment. Missing
dependencies block the run: report installation instructions rather than making
system-wide changes. Run `codex login` as needed and configure git author identity.
The controller uses configured inference services; “local” does not mean offline
or execution on Orin's Qwen model. Publishing additionally requires GitHub CLI,
`gh auth login`, and HTTPS push credentials such as `gh auth setup-git`.

Create `/data/orin-tasks/task.md` outside the repository with this approved task:

```text
Create docs/local-agent-smoke-test.md explaining the local coordinator workflow.
Use investigator to inspect tools/local-agent, implementer to write the document,
and verifier to independently check its accuracy.
Cover task input, isolated checkout, local checks, draft PR publication,
feedback continuation, and human lab acceptance.
Only change docs/local-agent-smoke-test.md. Do not change runtime code,
configuration or tests. Do not access the cluster, publish or deploy.
If specialist delegation is unavailable, report blocked.
```

Save the following as `/data/orin-tasks/smoke-checks.json`. Checks are JSON argument
arrays, not shell strings; no shell expansion or pipelines occur. They execute
from the isolated checkout and are captured in run state before the agent starts.

```json
[
  ["python", "-m", "unittest", "discover", "-v", "-s", "tools/local-agent/tests"],
  ["python", "-c", "from pathlib import Path; p = Path('docs/local-agent-smoke-test.md'); assert p.is_file() and p.read_text().strip(), 'Missing or empty document'"]
]
```

## Run locally and inspect evidence

From a checkout containing the controller, the human may start a new run:

```bash
python tools/local-agent/run.py \
  --task /data/orin-tasks/task.md \
  --checks /data/orin-tasks/smoke-checks.json \
  --run /data/orin-agent-runs/local-agent-smoke
```

Keep the run directory outside the source repository. The controller clones
`https://github.com/ILGSupreme/Orin.git` at remote `main` by default, then creates
an `agent/<random>` branch in `<run>/checkout`. It does not copy uncommitted files
from the invoking checkout. Override `--repo owner/name` and `--base branch` if
needed; the base must already exist remotely and becomes the PR target. Before
this tooling merges, use its existing remote feature branch as the base. The
original task and check arrays are saved in `state.json`.

Omitting `--publish` prevents controller GitHub calls and pushes. It still clones
over the network, runs Codex, executes checks, runs `git diff --check`, stages all
changes, and creates a local candidate commit if needed. The coordinator and its
specialists must not commit or publish: the controller owns those operations.
The default limits are 45 minutes per coordinator invocation and 15 minutes per
check, adjustable through `--minutes` and `--check-minutes`.

Inspect `<run>/state.json` and `<run>/attempt-N/`. Each attempt records
`prompt.txt`, `schema.json`, `agent.jsonl`, and, when produced, `result.json`,
`check-0.log`, `check-1.log`, and `pr-body.md`. Agent logs and task/feedback text
may contain sensitive information; review and redact evidence before sharing.

The coordinator returns `status`, a single-line `title`, and a PR-description
`body`. The body must include acceptance criteria, changes, actual test evidence,
independent review outcome, limitations, and precise HUMAN lab steps. `ready`
means ready for CI and human evaluation, never live-lab acceptance. The controller
then independently reruns the captured checks. `local_ready` records a local
candidate and PR body; `awaiting_human` records draft publication, not passing CI
or lab tests. A successful run with no diff against the base is `no_changes` and
creates no PR. Failures produce `blocked`; inspect the available result and logs.
Timeout or interruption stops the subprocess group and leaves partial files for
inspection. One process may own a run at a time.

## Optional draft publication and feedback

Publication is a separate human decision. Only an operator explicitly authorizing
push and draft publication should use `--publish`; do not run it for this approved
local-only task. On an authorized run, adding that flag makes the controller push
the candidate branch and create a draft PR after a ready result and passing
checks. It never builds images, deploys, merges, or grants human acceptance.

An existing run requires `--feedback`. Save concrete findings and requested
within-scope changes in `/data/orin-tasks/feedback.md`, then continue locally:

```bash
python tools/local-agent/run.py \
  --run /data/orin-agent-runs/local-agent-smoke \
  --feedback /data/orin-tasks/feedback.md
```

Use the original `--repo` again if it was overridden. Each attempt starts a new
coordinator conversation with the saved original task, current checkout, and
supplied feedback. Include relevant previous findings explicitly; the previous
conversation is not automatically restored. Captured checks rerun, and a new
candidate commit is recorded if changes exist. Adding `--publish` to an
explicitly authorized continuation updates the same open PR and returns it to
draft if necessary. Without that flag, an existing remote PR is not updated.
There is no automatic CI repair loop or force-push. Remote branch divergence can
fail a push and needs human reconciliation; closed or merged PRs require a new
run. Inspect `blocked` state before recovering rather than deleting evidence or
weakening checks.

## HUMAN evaluation checklist

1. Review the candidate diff and confirm that only this document changed. Read
   the task and resulting acceptance criteria; verify all three specialist roles
   actually ran and final review followed completed edits.
2. Run `python -m unittest discover -v -s tools/local-agent/tests` and the exact
   document-presence check from the JSON above in the candidate checkout. Inspect
   both check logs and the recorded candidate SHA. Controller tests simulate
   Codex/GitHub calls; their success does not validate authentication or a real
   end-to-end run.
3. For a separately authorized authenticated smoke run, invoke the local command
   above with a fresh run directory and an appropriate remote base. Confirm the
   isolated branch, original checkout isolation, local commit, `local_ready`, and
   PR body. No draft should be published without `--publish`. Exercise feedback
   on the same run and confirm a new attempt uses the original checks. Publication
   and same-PR behavior require separate authorization and real-service testing.
4. For an authorized published candidate, wait for CI on the exact candidate
   commit and record its results. Review the draft manually. This documentation
   change requires no image build, deployment, or hardware test; record those as
   not applicable, not as passed.
5. For future runtime tasks using this workflow, the human must build the recorded
   candidate commit using the existing lab procedure, record its actual image
   digest, deploy it personally, and execute the PR's concrete lab steps. Record
   commit, digest, configuration, observations, and pass/fail evidence. Never
   invent an image digest or infer live-model behavior from local unit tests.
6. Feed failures back explicitly. After any candidate change, obtain fresh CI and
   applicable human acceptance for that candidate. Only the human reviews and
   merges; neither coordinator confidence nor `awaiting_human` grants acceptance.

Implementation references: [controller and workflow](../tools/local-agent/README.md),
[controller source](../tools/local-agent/run.py),
[coordinator instructions](../tools/local-agent/coordinator.md), and
[local controller tests](../tools/local-agent/tests/test_runner.py).
