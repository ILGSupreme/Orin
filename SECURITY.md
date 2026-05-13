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

Contact:

```text
security@nordavind.se
```

If that address does not exist yet, replace it with your current private contact
email until Nordavind has a dedicated security address.

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
- memory/session persistence
- model-loading and Hugging Face download flows
- container and registry configuration
- authentication, authorization, and secret handling

## Out of scope

The following are generally out of scope unless they create a vulnerability in
Orin itself:

- vulnerabilities in third-party models
- unsafe model output
- vulnerabilities in external services
- issues caused by unsupported local modifications
- denial-of-service from intentionally overloaded local hardware

## Disclosure

Nordavind will make a best-effort attempt to acknowledge valid reports, assess
impact, and provide a fix or mitigation.

Responsible disclosure is appreciated.
