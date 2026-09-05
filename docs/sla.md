# Support & SLA policy (template)

Aether-Vault is self-hosted software, not a hosted service — this document is a
**template an operator or vendor fills in for their own deployment/support contract**,
not a live commitment Aether-Vault itself makes to every user. What IS a live commitment
is `SECURITY.md`'s vulnerability-report acknowledgment window; everything below is a
starting point for a real commercial support agreement.

## Support tiers (template)

| Tier | Response target (first response) | Channels |
|---|---|---|
| Community (OSS) | Best effort, no SLA | GitHub Issues, GitHub Discussions |
| Standard (commercial) | 1 business day | Private issue tracker / email |
| Priority (commercial) | 4 business hours, 24/7 for Sev-1 | Email + on-call escalation path |

Fill in real contact channels and hours before using this as an actual customer-facing
document — placeholders above are illustrative, not live.

## Severity definitions

| Severity | Definition | Example |
|---|---|---|
| Sev-1 | Registry down or data-loss risk in production | `/api/ready` failing cluster-wide; a failed restore |
| Sev-2 | Major feature broken, workaround exists | Webhooks not delivering; one HA replica down |
| Sev-3 | Minor issue, no immediate customer impact | A CLI flag's help text is wrong |

## What backs these commitments (real, shipped tooling)

- `av support-bundle` — a redacted diagnostics artifact (versions, health/ready,
  container status, a speed probe) a customer can hand to support without a
  back-and-forth for basic facts.
- `av admin backup create/verify/restore` + the real destroy-and-restore drill
  (`docs/dr.md`) — what actually backs a Sev-1 data-recovery commitment.
- `scripts/ha_drill.sh` — what actually backs an availability commitment for the HA
  topology specifically (not a claim without a drill behind it).
- `docs/runbooks/` — the procedures a support engineer or on-call operator actually
  follows for the incidents this policy exists to cover.

## What this does not cover

Reference-customer relationships and an actual staffed support rotation are commercial/
hiring outcomes this document cannot manufacture — see `todo.md`'s own framing of the
same point for the reference-customer/pilot-onboarding side.
