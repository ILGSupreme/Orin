# Continuous integration

`.github/workflows/ci.yml` runs on pull requests, pushes to `main`, and manual
dispatch. The stable job name is **CPU checks**. It uses a fresh GitHub-hosted
Ubuntu 24.04 runner for each of Python 3.10 (matching current images) and 3.12,
with a 15-minute timeout. The **CPU checks** gate requires both versions and the
separate native grammar job to pass.
A newer run for
the same PR/ref cancels the older run.

The workflow compiles Python sources, checks selected runtime modules for Ruff
`F` and `E9` correctness errors, and discovers all `test_*.py` tests in `src/test`.
The lint scope is deliberately limited to the agent work; it does not certify
the whole repository against all Ruff rules. Bash pipeline failures propagate
through `tee`, and failed checks upload available logs for seven days.

These tests use real Memory handlers and temporary SQLite databases with model,
provider HTTP and tool doubles. Kubernetes discovery also exercises the installed
client against a local HTTP server, including EndpointSlice decoding, omitted/null/
empty endpoints, mixed ready/empty slices and API errors. Discovery uses generated
routing/authentication with raw JSON decoding to avoid typed-client rejection of
empty slices. Its dependency version is pinned identically in CI and Cortex. No GPU, model weights,
provider credentials, running cluster, or paid API calls are required. Hugging
Face Hub is configured offline during checks. Live federation smoke scripts are
not included in unit-test discovery.

The **Native GGUF grammar** job builds CPU-only llama-cpp-python 0.3.19 and runs
`PYTHONPATH=src python src/test/gguf_grammar_smoke.py`. It invokes the actual native
parser for TurnPlan and Decision; conversion to a Python grammar object alone
does not prove native compilation. This job has a 20-minute build timeout and
requires no GPU, model weights or credentials. It validates the reference CPU
runtime; the actual Jetson/gateway build and model behavior still need lab checks.

## Run locally

From the repository root, using Python 3.10 or 3.12:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-test.txt
export PYTHONPATH=src
export HF_HUB_OFFLINE=1
python -m compileall -q src terminal-chat.py
python -m unittest discover -v -s src/test -p 'test_*.py'
```

For the focused lint command, use the workflow's **Check runtime correctness**
step. Shared dependency pins in `requirements-test.txt` should stay aligned with
Cortex runtime requirements. Test dependencies exclude native inference engines.
Actions are pinned to commit SHAs; update those pins with their version comments
when adopting upstream action releases.

## Merge gate and live acceptance

The aggregate **CPU checks** job requires both Python matrix jobs and the native
grammar job to succeed. A repository administrator must configure it as a required
check in the branch ruleset; the workflow itself does not enable protection.
This documentation review did not verify the repository's active ruleset.

Use workflow results for the exact reviewed commit as evidence. Passing these
checks does not establish that the corresponding images have been deployed or
that a real model completes tasks reliably. Track live acceptance separately
from whether implementation has merged; see [the runtime](cortex-agent.md).

Next, add hosted ARM64 tests and AMD64/ARM64 container build checks. These verify
portability and packaging, but do not substitute for Jetson/CUDA testing.

Use a separate controlled workflow for reviewed revisions and dedicated hardware
runners when hardware/cluster access is available. Do not execute public PR code on persistent
Jetson/GPU runners with cluster access. Ordinary PR checks use a read-only token,
do not retain checkout credentials, and receive no lab or provider secrets.

For future projects, extract shared jobs into reusable `workflow_call` workflows
once their common requirements are established. Each repository should retain
ownership of its test commands; hardware runner access stays separately scoped.

## Evidence for a lab run

Record the source commit, image digests, architecture, Python/native dependency
versions, model and inference settings, configuration changes and test inputs.
Correlate turns, goals, transient jobs, actions, observations and persisted
outcomes. Check both accepted work and its actual completion; a conversational
acknowledgement is not execution evidence.

Repeat representative requests to assess model behavior. Re-run the Federation
submit/poll smoke flow against the selected deployment, including result
sanitization. Keep historical observations dated rather than treating them as
a current regression result. Test recovery with preserved Memory data and
explicitly reconcile uncertain in-flight work.
