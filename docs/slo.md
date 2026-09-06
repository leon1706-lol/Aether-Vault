# Service level indicators & objectives

Self-hosted numbers a deployment can hold ITSELF to and measure with the tooling this
repo ships — not a hosted-SaaS commitment (Aether-Vault is self-hosted; there is no
Aether-Vault-operated multi-tenant cloud this doc could speak for).

**Honest status (updated v1.3.4 — this section was stale since v1.3.3 shipped the
endpoint it describes as missing):** `GET /api/metrics` is real — an `admin`-scoped
Prometheus text-exposition endpoint (`python/av_server/metrics.py`, wired in `server.py`)
exporting per-(method, path template, status class) request counts and duration
histograms, plus point-in-time webhook queue depth and DB pool checkout snapshots.
**Per-process only, honestly documented as such in `metrics.py`'s own module docstring**:
a multi-replica deployment scrapes each replica independently (Prometheus's normal
multi-target model) — there is no cross-replica aggregation here, so an HA topology's
*combined* SLI still comes from `scripts/ha_drill.sh`'s own drill, not from summing
per-replica scrapes by hand. The SLIs below are measured via a mix of that live endpoint,
`av doctor --speed`, `scripts/e2e_scenario.sh`'s live timing assertions,
`tests/test_perf_gate.py`'s hot-path budgets, and `scripts/ha_drill.sh`/the DR drill's
printed numbers.

## SLIs and targets

| SLI | Target | How it's measured today |
|---|---|---|
| Commit push latency (single small commit, warm connection) | p50 < 300ms, p95 < 1.5s | `tests/test_perf_gate.py`'s `log()`/hot-path budgets (CPU class); `av doctor --speed` locally |
| `/api/ready` response time | < 200ms | `GET /api/metrics`'s duration histogram for that path, per replica; manual/CI curl timing as a cross-check |
| Webhook delivery latency (first attempt, healthy subscriber) | < 5s from event to POST | `_WEBHOOK_TIMEOUT_SECS`-bounded by construction; `/api/metrics`'s webhook queue depth snapshot shows backlog, not per-delivery latency — no dashboard for the latter yet |
| Availability under a single replica failure (HA topology) | Zero failed client requests during a replica kill | `scripts/ha_drill.sh` — a real, repeatable local drill, not a claim |
| Backup restore time (RTO) | Measured per run, not a fixed promise | `scripts/e2e_scenario.sh` Phase U prints the actual wall-clock seconds — see `docs/dr.md` |
| GC sweep correctness | Zero live objects deleted, orphans reclaimed within one grace window | `tests/test_server.py` GC tests + e2e Phase E |

## Error budget

No SLI above has a tracked *historical* error-budget burn-down dashboard — `/api/metrics`
exports current-process counters/histograms for a live Prometheus scrape to build one
from, but nothing in this repo runs Prometheus/Grafana itself or persists a time series
(out of scope for a self-hosted, bring-your-own-observability-stack project). Until an
operator wires that up, the operational signal is: CI stays green (`tests.yml`'s full
matrix, `ha-drill`, `security.yml`), and `development/Probleme.md` is where a real
production incident's root cause gets recorded — treat a growing, unaddressed
Probleme.md as the error budget actually burning.

## What would close the remaining gap

The endpoint and its request-rate/latency counters are built (v1.3.3, WP-35) — the
remaining gap is genuinely operational, not code: this repo ships no example
Prometheus scrape config / Grafana dashboard JSON, and there is still no cross-replica
aggregation (each replica's counters are its own, by design — see the endpoint's own
docstring). A follow-up could add a documented `prometheus.yml` scrape-config snippet
(`bearer_token_file` pointed at an admin-scoped token) and a starter dashboard under
`docs/` — tracked in `todo.md` as a CI/CD-observability-adjacent item, not blocking this
plan's own CI-pipeline-observability work (`ci-summary`, which reports on THIS repo's own
pipeline, not on a deployed instance).
