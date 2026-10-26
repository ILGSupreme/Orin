# Working on Orin

Read CONTRIBUTING.md and docs/ci.md before changing code. Scope work to the
requested task and use repository checks appropriate to that change. Follow the
reviewed local environment; other CI versions, native grammar checks and hardware
acceptance may remain separate and must be reported explicitly.

The development coordinator is the standalone ILGSupreme/dev-agent repository.
Do not recreate it or commit generated Codex role/hook preparation files in Orin.

Do not access the live cluster unless the user explicitly authorizes the relevant
read-only inspection. Deployment, image builds/pushes, cluster mutations and
model downloads require separate authorization. Local test success does not
establish live model behavior, Jetson compatibility or human lab acceptance.
Distinguish observed results from assumptions and preserve historical evidence.
