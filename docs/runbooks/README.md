# Runbooks

Operational procedures for the incidents/tasks `docs/sla.md`'s severities and
`docs/slo.md`'s SLIs actually reference. Each one names the real command that does the
work — none of these are aspirational.

- [`incident-response.md`](incident-response.md) — first steps when something's down.
- [`ha-failover.md`](ha-failover.md) — a replica or a Postgres node goes down under the
  HA topology.
- [`dr-restore.md`](dr-restore.md) — restoring from backup after real data loss.
- [`tenant-provisioning.md`](tenant-provisioning.md) — onboarding a new tenant.
- [`upgrade-rollback.md`](upgrade-rollback.md) — upgrading the engine image/schema, and
  rolling back if it goes wrong.
