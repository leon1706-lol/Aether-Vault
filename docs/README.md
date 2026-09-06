# Docs index

| Doc | What it covers |
|---|---|
| [`tutorial.md`](tutorial.md) | One continuous operator + agent path: init → train under a run → env snapshot → commit → promote past a policy gate → publish a handoff → the next agent picks it up. |
| [`rsi-operator-guide.md`](rsi-operator-guide.md) | The RSI control plane's continuous path: register/propose/apply an improver self-edit → sandbox it → canary it → dual-gate promotion (denied, then reviewed and allowed) → record lessons/strategy/lineage → budgets and freeze/rollback. |
| [`for-agents.md`](for-agents.md) | The minimal recipe for driving Aether-Vault from an autonomous loop (CLI subprocess or `av_sdk.Repo` — both equivalent), plus the shared error/exit-code registry. |
| [`contracts.md`](contracts.md) | Every published, versioned JSON Schema (envelope, semantic diff, `.avh`, event, run, webhook payload) and where each is produced. |
| [`avattributes.md`](avattributes.md) | `.avattributes` staging directives — forcing or suppressing chunking/layer-splitting for a specific path. |
| [`migrate-engine-image.md`](migrate-engine-image.md) | Moving a pinned two-container (`aether-vault-server`/`aether-vault-webui`) compose file onto the consolidated `aether-vault-engine` image, or onto the new slim single-role images. |
| [`dr.md`](dr.md) | Disaster recovery: `av admin backup create/verify/restore`, the real destroy-and-restore drill (Phase U), and the measured-RTO/stated-RPO distinction. |
| [`enterprise-operator-guide.md`](enterprise-operator-guide.md) | Identity/RBAC/tenancy/DR continuous path: provision a tenant → create users → grant roles → mint a remote token → turn on tenant enforcement → back up and restore, plus SSO (OIDC/SAML) and SCIM provisioning. States plainly what's still NOT done (a live run against a real external IdP). |
| [`support.md`](support.md) | Where to get help, `av support-bundle`, and links to `sla.md`/`slo.md`/`runbooks/`. |
| [`slo.md`](slo.md) | Service level indicators/objectives and how each is actually measured today, including the live `GET /api/metrics` Prometheus endpoint. |
| [`sla.md`](sla.md) | A support/SLA policy TEMPLATE — fill in real tiers/contacts before using it as a live commitment. |

For everything else — install, CLI reference, architecture, CI, benchmarks, the full
build history — see the top-level [`README.md`](../README.md) and `development/` (start
with [`architecture.md`](../development/architecture.md) and
[`infrastructure.md`](../development/infrastructure.md)).

`tests/test_docs_commands.py` parses every fenced `av ...` command on every page in this
directory and asserts the command and each flag actually exist in the live Click tree —
these docs can't silently drift from the real CLI.
