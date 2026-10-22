# Security Policy

## Supported versions

Orin is currently in early development. Security fixes are applied to the main
development branch until formal releases are introduced.

| Version | Supported |
|---|---|
| main | Yes |
| tagged releases | Not yet available |

## Reporting a vulnerability

Please report security issues privately.

Do not open a public GitHub issue for vulnerabilities.

The repository currently lists `security@nordavind.se` as its private contact.
Mailbox availability and monitoring have not been confirmed. Maintainers need
to confirm this address before it can be treated as a verified reporting channel.
Do not publish vulnerability details or credentials in an issue while arranging
private contact.

## What to include

Please include as much of the following as possible:

- affected component
- affected version, commit, or branch
- deployment environment
- reproduction steps
- expected impact
- logs or traces, if safe to share
- suggested fix, if known

## Scope

Security-relevant areas include:

- Cortex ingress and command handling
- Kubernetes deployment operations
- node SSH/bootstrap operations
- service discovery and routing
- WorkPacket execution
- Memory conversation, goal, observation and node-inventory persistence
- execution leases, revision checks, recovery and evidence validation
- external inference privacy, context selection and provider credentials
- Federation membership, signatures, replay protection, policy and result polling
- model-loading and Hugging Face download flows
- container and registry configuration
- authentication, authorization, and secret handling

## Trust boundaries and current limitations

Cortex administration, goal controls and Memory APIs assume a trusted local
environment. User/session and namespace scoping are not authentication.
Explicit terminal administration commands are separate from the agent's
allowlisted read-only actions.

Federation is the intended cross-environment entry point. Joining a network or
discovering a capability grants no arbitrary Cortex, Memory, SSH or Kubernetes
access. The current Cortex WorkPacket ingress accepts LLM work only.

The HTTP Federation MVP implements signature and policy checks, but network/token
administration and result polling still require authorization hardening before
untrusted public exposure. Policy fields alone do not establish enforced quotas
or concurrency limits. Readiness responses do not certify these security
properties.

External inference requires operator enablement and an external-allowed privacy
policy. Model output cannot grant that permission. Do not include keys, tokens,
private model inputs or unsanitized provider responses in reports or logs.

## Out of scope

The following are generally out of scope unless they create a vulnerability in
Orin itself:

- vulnerabilities in third-party models
- model answer quality alone (permission bypass, private-data disclosure and
  fabricated completion accepted across an execution boundary remain in scope)
- vulnerabilities in external services
- issues caused by unsupported local modifications
- denial-of-service from intentionally overloaded local hardware

## Disclosure

Nordavind will make a best-effort attempt to acknowledge valid reports, assess
impact, and provide a fix or mitigation.

Responsible disclosure is appreciated.
