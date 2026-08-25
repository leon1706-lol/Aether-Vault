# V1.2.0 — COMPLETE ✅ (2026-08-25)

This pause-point document is RETAINED as the milestone record; all items below are DONE.

## Final verification snapshot
- Stack-free battery: **421 passed / 0 failed** (60 by-design skips)
- Live-stack suite (embedded PG 15 + redis sink): **103 passed / 0 failed**
- E2E scenario: **8/8 phases PASS** (A collab · B offline · C legacy heal · D per-user auth ·
  E GC · F SDK loop · G event reactiveness · H promotion policy)
- WebUI: Vitest 88/88 · tsc clean · eslint clean
- eager-annotation checker: 29 files, 0 problems · workflows YAML valid

## Shipped in V1.2.0 (see CHANGELOG Phase 54)
M1 agent plumbing (JSON envelope+exit codes, av_sdk.Repo, events+webhooks, .avh v2) ·
M2 runs first-class + semdiff + context memory + env replay + CDC extensions ·
M3 --no-upload / policies / promote / watch · M4 RunsPanel + audit + registry export +
HMAC attest · M5 docs repositioning (README, AGENTS.md, docs/for-agents.md).

## Post-v1.2.0 backlog (not started, by design)
- Server-side policy enforcement (authz model) — enterprise tier
- Asymmetric signing (ed25519) replacing HMAC attestation spike
- Public HTTP contract doc for community TS/Rust SDKs (v1.3+)
- Benchmark #5 capture + full cross-tool re-run post-perf-work (ops task,
  see infrastructure.md stack-up notes)
