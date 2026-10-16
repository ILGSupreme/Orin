# Continuous integration

`.github/workflows/ci.yml` runs on pull requests, pushes to `main`, and manual
dispatch. The stable job name is **CPU checks**. It uses a fresh GitHub-hosted
Ubuntu 24.04 runner for each of Python 3.10 (matching current images) and 3.12,
with a 15-minute timeout. The **CPU checks** gate requires both versions to pass.
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

## Merge gate and later stages

After the first successful run, a repository administrator should add **CPU
checks** as a required status check in the `main` branch ruleset. The workflow
alone does not enable branch protection. Keep the PR draft until the separate
live model/provider/cluster validation described in `cortex-agent.md` is complete.

Next, add hosted ARM64 tests and AMD64/ARM64 container build checks. These verify
portability and packaging, but do not substitute for Jetson/CUDA testing.

When the lab exists, use a separate controlled workflow for reviewed revisions
and dedicated hardware runners. Do not execute public PR code on persistent
Jetson/GPU runners with cluster access. Ordinary PR checks use a read-only token,
do not retain checkout credentials, and receive no lab or provider secrets.

For future projects, extract shared jobs into reusable `workflow_call` workflows
once their common requirements are established. Each repository should retain
ownership of its test commands; hardware runner access stays separately scoped.
