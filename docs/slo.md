# Service level indicators & objectives

Self-hosted numbers a deployment can hold ITSELF to and measure with the tooling this
repo ships — not a hosted-SaaS commitment (Aether-Vault is self-hosted; there is no
Aether-Vault-operated multi-tenant cloud this doc could speak for).

**Honest status**: there is no live `/api/metrics` (Prometheus) endpoint yet — that is a
real, tracked gap (`todo.md`), not implied otherwise. The SLIs below are measured today
via `av doctor --speed`, `scripts/e2e_scenario.sh`'s live timing assertions,
`tests/test_perf_gate.py`'s hot-path budgets, and `scripts/ha_drill.sh`/the DR drill's
printed numbers — real measurements, just not continuously exported as a scrape target
yet.

## SLIs and targets

| SLI | Target | How it's measured today |
|---|---|---|
| Commit push latency (single small commit, warm connection) | p50 < 300ms, p95 < 1.5s | `tests/test_perf_gate.py`'s `log()`/hot-path budgets (CPU class); `av doctor --speed` locally |
| `/api/ready` response time | < 200ms | Manual/CI curl timing; no continuous scrape yet |
| Webhook delivery latency (first attempt, healthy subscriber) | < 5s from event to POST | `_WEBHOOK_TIMEOUT_SECS`-bounded by construction; no dashboard yet |
| Availability under a single replica failure (HA topology) | Zero failed client requests during a replica kill | `scripts/ha_drill.sh` — a real, repeatable local drill, not a claim |
| Backup restore time (RTO) | Measured per run, not a fixed promise | `scripts/e2e_scenario.sh` Phase U prints the actual wall-clock seconds — see `docs/dr.md` |
| GC sweep correctness | Zero live objects deleted, orphans reclaimed within one grace window | `tests/test_server.py` GC tests + e2e Phase E |

## Error budget

No SLI above has a tracked historical error-budget burn-down dashboard yet (that needs
the metrics endpoint this doc already flags as missing). Until then, the operational
signal is: CI stays green (`tests.yml`'s full matrix, `ha-drill`, `security.yml`), and
`development/Probleme.md` is where a real production incident's root cause gets recorded
— treat a growing, unaddressed Probleme.md as the error budget actually burning.

## What would close this gap

A real `/api/metrics` (Prometheus text exposition, hand-rolled with no new dependency —
matching the judgment call `rate_limit.py` already made) needs request-rate/latency
counters wired into the ASGI middleware stack. That stack's ordering is explicitly
documented as fragile (`server.py`'s own "registration order: auth → CORS → rate limit;
runtime order: rate → CORS → auth → routes" comment) — adding a new middleware layer
there deserves its own careful pass with full live re-verification of every
authentication/CORS/rate-limit interaction, not a rushed addition. Tracked in `todo.md`.
