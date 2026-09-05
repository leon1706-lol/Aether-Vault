# Support

Where to get help, and what to gather before you ask.

## Community support (no SLA)

- **Bugs**: [GitHub Issues](https://github.com/leon1706/aether-vault/issues) — include
  `av --version`, your deployment shape (compose/Helm/bare), and (ideally) an
  `av support-bundle` output (see below — it redacts every credential before writing).
- **Questions/discussion**: GitHub Discussions.
- **Security reports**: **not** a public issue — see `SECURITY.md`'s private
  vulnerability reporting process.

## Commercial support

See [`docs/sla.md`](sla.md) — a template for a real support/SLA agreement; fill in real
tiers/contacts before treating it as a live commitment.

## Before you file anything: `av support-bundle`

```bash
av support-bundle
```

Writes a redacted diagnostics bundle (`bundle.json`) to a fresh directory: CLI/server
versions, `/api/health` + `/api/ready` output, container status and a log tail (when
Docker is reachable), your repo's config (every token/password/secret-shaped value
replaced with `***REDACTED***` before anything touches disk), and a local speed-probe
snapshot. Attach `bundle.json` to a support request instead of pasting logs by hand.

```bash
av support-bundle ./my-bundle    # write to a specific directory instead of a timestamped default
```

## Runbooks

Operational procedures for common incidents live in [`docs/runbooks/`](runbooks/) —
incident response, HA failover, DR restore, tenant provisioning, and upgrade/rollback.

## Diagnosing common issues yourself first

- `av auth doctor` — Protected-mode onboarding: is a token configured? Is the server
  reachable? Does the token actually authenticate?
- `av doctor` — repo-local diagnostics (native core availability, index/pointer
  consistency, pending-push queue).
- `av doctor --speed` — a read-only timing snapshot if something feels slow.
- `docs/slo.md` — what "healthy" is supposed to look like, and how it's measured today.
