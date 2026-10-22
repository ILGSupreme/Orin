# Local coordinator smoke test

This guide exercises the local task-to-review workflow in
[`tools/local-agent`](../tools/local-agent/README.md). The controller creates an
isolated checkout, invokes a coordinator and named specialists, checks their
reports, and runs local checks. A successful local run is ready for CI and human
evaluation; it is not live lab acceptance.

The commands below are instructions for an operator, not actions performed by
this documentation task. Do not access the cluster, build/push images, deploy,
merge or publish while carrying out the documentation-only task.

## Prerequisites and boundaries

Use Linux, Python 3.10+, git, and a locally authenticated current Codex CLI.
The CLI must support the project agent roles, `agents.enabled`,
`agents.max_concurrent_threads_per_session`, `exec --output-schema`, the
`workspace-write` sandbox, and `SubagentStart`/`SubagentStop` hooks. Missing
specialist delegation or unsupported/untrusted hooks must leave a run blocked.
For optional publication, install GitHub CLI and configure `gh auth login`,
`gh auth setup-git`, and git author identity. Authenticate Codex with
`codex login` before running the workflow.

Use a development environment without cluster write credentials, deployment
SSH keys or Docker socket access. Specialists share the coordinator's account,
permissions and checkout; role instructions and hooks are not a separate
security boundary. Inherited MCP configuration does not authorize its use.

The repository recommends an activated virtual environment with
[`requirements-test.txt`](../requirements-test.txt) installed; subprocesses
inherit its PATH. If dependencies are missing, report blocked and have the
operator install them in a virtual environment, for example from the checkout:

```bash
python3 -m venv /tmp/orin-local-agent-venv
source /tmp/orin-local-agent-venv/bin/activate
python -m pip install -r requirements-test.txt
```

Do not install system-wide dependencies or weaken checks. The smoke checks below
exercise controller tests and document presence; they do not replace the broader
CPU checks in [`checks.example.json`](../tools/local-agent/checks.example.json),
hosted CI, native builds or hardware tests for runtime changes.

## Task input and acceptance criteria

Keep task files and run directories outside the source checkout, such as under
`/data/orin-tasks` and `/data/orin-agent-runs`. Create a task file at
`/data/orin-tasks/local-agent-smoke-test.md` containing:

```text
Create docs/local-agent-smoke-test.md explaining the local coordinator workflow.
Only change docs/local-agent-smoke-test.md. Do not change runtime code,
configuration or tests. Use investigator to inspect tools/local-agent,
implementer to write the document, and verifier to independently check accuracy.
Cover task input, isolated checkout, local checks, draft PR publication,
feedback continuation, and human lab acceptance. Do not access the cluster,
publish or deploy. If specialist delegation is unavailable, report blocked.
```

The coordinator must turn that scope into explicit acceptance criteria:

- Only `docs/local-agent-smoke-test.md` changes.
- Commands and state descriptions match the controller and explain all six
  workflow stages named in the task.
- The investigator, implementer and final verifier complete independent role
  reports; verification happens after investigation and implementation finish.
- Both configured checks pass without modification, and the final response
  records actual test evidence, limitations and human acceptance steps.

Write `/data/orin-tasks/local-agent-smoke-checks.json` with exactly these argument
arrays:

```json
[
  ["python", "-m", "unittest", "discover", "-v", "-s", "tools/local-agent/tests"],
  ["python", "-c", "from pathlib import Path; p = Path('docs/local-agent-smoke-test.md'); assert p.is_file() and p.read_text().strip(), 'Missing or empty document'"]
]
```

The controller executes each array without a shell. It copies the task and checks
into `state.json` before invoking the agent. Editing the original checks file
later does not change checks for that run; choose a new run for a new check set.
An investigation request alone does not authorize fixes; an audit should request
a repository findings document explicitly.

## Prepare and trust an isolated checkout

From a checkout containing the controller, run:

```bash
python tools/local-agent/run.py \
  --task /data/orin-tasks/local-agent-smoke-test.md \
  --checks /data/orin-tasks/local-agent-smoke-checks.json \
  --run /data/orin-agent-runs/local-agent-smoke-test \
  --prepare
```

The default repository is `ILGSupreme/Orin` and base is `main`. Override `--repo`
and `--base` on the initial command when needed. The base must already exist
remotely and contain the tooling, roles and hooks being tested: the controller
clones the remote base, not uncommitted files from the launching checkout.
It records the base commit, creates an `agent/<id>` branch in `RUN/checkout`,
and stops in `prepared` state without invoking the coordinator.

Open Codex inside that isolated checkout, review/trust the project configuration,
and use `/hooks` to review both lifecycle hooks. Exit Codex. Hook trust is not
bypassed automatically; if the CLI lacks hook support, retain its diagnostic and
report blocked. Repeat the command without `--prepare` to start the first attempt.
A prepared run does not require feedback.

The default agent invocation limit is 45 minutes and each controller check has
15 minutes; `--minutes` and `--check-minutes` override them. These are execution
timeouts, not spending limits. Only one controller process may own a run.

## Specialist reports and local checks

Follow [`coordinator.md`](../tools/local-agent/coordinator.md) and the named
[investigator](../.codex/agents/investigator.toml),
[implementer](../.codex/agents/implementer.toml), and
[verifier](../.codex/agents/verifier.toml) roles. Use at most three concurrent
agents, no recursive delegation, disjoint file ownership and serialized
overlapping edits. For this task only the implementer owns the document.
Wait for actual investigator and implementer reports and stop all workers before
starting the final verifier. All edits must finish before final review. If a
verified blocker needs repair, finish the repair and start a fresh verifier;
allow at most two repair rounds. Do not edit tracked or nonignored files after
verification.

Each specialist returns plain JSON with `verdict` (`pass` or `blocked`), a
nonempty `summary`, and a nonempty string list `evidence`. Passing means that
role completed its assigned work, not that a human accepted the change.
The coordinator returns `ready` or `blocked`, a concise title, and a PR body
containing acceptance criteria, changes, actual check evidence, independent
review outcome, limitations and precise HUMAN lab steps.

Hooks write `attempt-N/specialists.jsonl`; the controller verifies current-attempt
parent/checkout identities, distinct named specialists, passing reports and final
review ordering. It saves `verified-specialists.json` itself. Never create or
modify these evidence files manually. Missing, stale, malformed or skipped
reports/hooks block completion. The verifier's checkout fingerprint must match
after coordinator exit and controller checks. Hook behavior still needs a real
run on the installed CLI; simulated controller tests cannot establish it.

Run the configured checks locally from the isolated checkout:

```bash
python -m unittest discover -v -s tools/local-agent/tests
python -c "from pathlib import Path; p = Path('docs/local-agent-smoke-test.md'); assert p.is_file() and p.read_text().strip(), 'Missing or empty document'"
```

These are automated local checks, not a human-run CLI smoke test.
[`test_runner.py`](../tools/local-agent/tests/test_runner.py) mocks Codex,
GitHub/git commands and lifecycle validation in its workflow scenarios, including
publication and feedback. [`test_evidence.py`](../tools/local-agent/tests/test_evidence.py)
validates synthetic lifecycle records and calls the capture function directly
against a temporary git checkout. Those tests exercise controller decisions and
evidence validation; they do not establish that the installed CLI emits real
hooks, that a PR was published, or that authenticated feedback continuation
worked. The document-presence check establishes only that this file is nonempty.
Record actual check results separately from any HUMAN CLI smoke-test results.

Use a validation record like this, replacing placeholders only with collected
evidence and leaving unperformed steps marked `unrun`:

```text
Candidate commit: <recorded SHA, or pending controller commit>
Automated unit tests: <command, exit status, test count and result>
Automated document-presence check: <command and exit status>
HUMAN local CLI smoke test: unrun | <CLI version, command, attempt and observations>
HUMAN draft publication smoke test: unrun | <command, PR URL, head/base and draft observations>
HUMAN feedback continuation smoke test: unrun | <command, attempts, before/after SHAs and observations>
Human acceptance: pending | <reviewer, tested commit and recorded result>
```

After a `ready` result, the controller independently validates lifecycle evidence,
reruns frozen checks and runs `git diff --check`. It stages changes and creates a
local candidate commit even when `--publish` is omitted. Agents must not commit.
For a nonempty change it records the candidate in `state.json` and writes
`attempt-N/pr-body.md`, with state `local_ready`. An empty diff against the
recorded base yields `no_changes` and no PR. Check failures, missing dependencies,
timeouts and evidence failures leave the run blocked; partial files remain for
inspection. Logs may contain private information: review before sharing.

## Optional draft PR publication and feedback

The documentation task does not authorize publication. A separate operator
choice to pass `--publish` authorizes the controller to push and create/update a
draft PR after the local gates. The controller uses a normal push, never a force
push. It does not build an image, deploy or merge. State `awaiting_human` means a
draft PR exists; it does not mean CI or hardware passed. State `local_ready`
means local candidate artifacts exist; coordinator `ready` alone is only its
structured recommendation before controller validation.

After the first attempt, write explicit CI, review or lab feedback to
`/data/orin-tasks/local-agent-feedback.md` and continue the same run:

```bash
python tools/local-agent/run.py \
  --run /data/orin-agent-runs/local-agent-smoke-test \
  --feedback /data/orin-tasks/local-agent-feedback.md
```

Add `--publish` only when the operator authorizes publication. Retain the original
`--repo` on continuation if it was overridden. The controller uses the saved
task, base, branch and frozen checks. Each attempt starts a new coordinator
conversation; include relevant earlier findings in feedback because previous
conversation history is not restored. Checks and specialist evidence must pass
again. Publication updates the same open PR and returns it to draft if needed.
Remote branch changes can cause push failure requiring reconciliation; a closed
or merged PR requires a new run. CI does not trigger automatic repairs.

## HUMAN evaluation and lab acceptance

For this documentation-only candidate, a human should:

1. Review the recorded candidate commit and confirm only this document changed.
2. Review local `attempt-N/pr-body.md`; if publication was separately authorized,
   read the draft PR and wait for applicable CI on that exact commit.
3. In an isolated development environment, perform prepare/trust and a local run
   against a remote base containing the tooling. Confirm actual lifecycle hooks,
   frozen checks, candidate commit, `pr-body.md` and `local_ready` state. An
   operator may separately authorize a draft publication and feedback smoke run.
4. Record the tested commit, CLI/environment versions, commands, observations and
   pass/fail. Report failures through an explicit feedback file.

For a separately authorized publication smoke test, use a disposable branch/run
and an approved repository. Amend the operator's task instructions to explicitly
authorize controller publication before invoking it with `--publish`; the
documentation-only task above forbids publication. Confirm `state.json` records
`awaiting_human`, its candidate commit matches the remote branch/PR head, and the
recorded PR URL opens a draft targeting the intended base. Retain the command,
candidate SHA, PR URL and observed draft/base/head as evidence.

For separately authorized feedback continuation, record that PR URL and head,
write a concrete document-only correction to the feedback file, and run the
continuation command with `--publish` under the same publication authorization.
Confirm a new attempt directory, fresh specialist reports and passing check logs,
the requested correction in the candidate, and the same open PR updated to the
new candidate head in draft state. Record before/after SHAs, attempt numbers,
check results and PR observations. A local continuation without `--publish` can
validate local artifacts only. Mark either smoke test as unrun unless these
observations were actually collected; unit-test success is not that evidence.

This documentation change requires no image build or live hardware deployment;
none is established by the local smoke checks. For future runtime candidates,
the human must additionally follow the existing lab build procedure for the
recorded commit, record the actual resulting image digest and configuration,
deploy personally, execute the PR's precise lab steps and record evidence and
pass/fail. Never invent a candidate digest. Any change after testing needs fresh
acceptance. Human review and merge remain separate from coordinator readiness.

Implementation references: [`run.py`](../tools/local-agent/run.py),
[`evidence.py`](../tools/local-agent/evidence.py), and
[hook configuration](../.codex/hooks.json).
