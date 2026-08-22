# Security Policy

Aether-Vault versions ML artifacts and runs a networked registry service — we take both
local-data-integrity and remote-attack-surface reports seriously.

## Reporting a vulnerability

**Please do not report security issues in public GitHub Issues.**

Use GitHub's private vulnerability reporting:
[Open a private security advisory](https://github.com/leon1706/aether-vault/security/advisories/new)
on this repository. This keeps the details confidential until a fix is ready.

Include what you can of: affected component (CLI / `av_server` / webui / packaging),
affected version (`av --version` or the tag), reproduction steps or PoC, and your assessment
of impact. You will get an acknowledgment within **72 hours**, a status update at least
every 7 days while a fix is in progress, and public credit in the fix's release notes
(unless you prefer to stay anonymous).

## Supported versions

Only the **latest tagged release line** receives security fixes:

| Version | Supported |
|---|---|
| latest `vX.Y.Z` on PyPI | ✅ |
| older tags | ❌ — upgrade via `pip install --upgrade aether-vault` |
| `master` HEAD | best effort (fixes land here first) |

## Scope guidance

**In scope:** anything that lets an unauthenticated/unauthorized party read, write, or
destroy repository data; path traversal in ref/hash handling; hash-verification bypasses;
unsafe deserialization; supply-chain issues in our published artifacts; credential leakage
in logs, config files, or the token handoff flow.

**Known, tracked limitations** (reports welcome, but please reference these so we can link
them): the registry currently ships with permissive CORS (`allow_origins=["*"]`) and no API
rate limiting, and the optional "Protected" mode is a single shared secret rather than
per-user auth. All three are explicit roadmap items (see the README's Open Source Roadmap)
intended to close before any shared/public deployment.

**Out of scope:** social engineering, brute-force against deployments you don't own,
vulnerabilities in Docker/Postgres/Redis themselves (report upstream), and self-hosted
misconfigurations that expose ports publicly without Protected mode enabled.

## Hardening recommendations for operators

Until per-user auth ships: run registries with `av auth set-token` ("Protected" mode),
keep Postgres/Redis off the host network (the release compose file already omits their
port mappings), and put TLS termination in front of uvicorn for any non-localhost use.
