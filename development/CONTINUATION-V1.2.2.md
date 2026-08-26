# V1.2.2 — COMPLETE ✅ (2026-08-26)

This pause-point document is RETAINED as the milestone record. The cycle finished in the
same session it resumed; final verification snapshot:

- Stack-free battery: **492 passed / 0 failed** (69 by-design skips)
- Live-stack suite vs embedded PostgreSQL 15 + TCP-probe Redis: **546 passed / 0 failed**
  (full battery incl. all new v1.2.2 live coverage)
- WebUI: Vitest **99/99** · `tsc --noEmit` clean · eslint clean
- E2E scenario script: **A–H + J + K PASS locally** (Phase I runs as the dedicated CI
  `e2e-engine-smoke` job; needs a Docker daemon by definition)
- eager-annotation checker: 34 files, 0 problems · all workflow/compose YAML parses ·
  repo-file link sweep clean (only pre-existing anchor-only "misses")
- Manual wire pass: keygen → snapshot → signed commit → push → clone → **VERIFIED in
  clone** → **`av replay <commit>` renders from the registry CAS** — plus tamper exit-15
  and unsigned-ok verdicts

Shipped scope and decisions are below (kept verbatim as the working record). Real bugs
found & fixed during the cycle: Probleme #78–#84. Explicit deferral: benchmark #5 measured
row + full cross-tool re-run (needs a Docker session).

---

**Task:** V1.2.2 Plan — Engine Image Consolidation + Architectural Gap Closure.
Owner decisions locked in: **ONE image, ONE container with all subservices inside it**
(vetoed the two-container topology) · **legacy alias tags kept for one transition cycle.**

The rest of this file is the original pause-point record: what landed in which order.
Delete/replace its status header with "COMPLETE" when the cycle finishes (same pattern
as CONTINUATION-V1.2.0.md).

---

## 1. LANDED (code complete, stack-free suite green except 3 fixed test updates)

### Part 1 — Engine consolidation (one image, one container)
- `Dockerfile` — multi-stage: py-builder (unchanged wheel logic) → web-builder
  (`node:20-bookworm-slim`, npm ci + build, standalone output) → runtime
  `python:3.12-slim` + NodeSource Node 20 + wheels + `/data` + standalone/static/public.
- `docker/engine-entrypoint.sh` — NEW supervisor. `AV_ENGINE_ROLE=all|server|webui`;
  default **all** runs uvicorn (:8000) foreground + node server.js (:3000) background;
  either child dying kills the container. **Legacy auto-detect**: unset role +
  `DATABASE_URL` set → server-only; `NEXT_PUBLIC_API_URL` set without DB → webui-only
  (keeps old pinned composes working against aliased legacy tags).
- `docker-compose.yml` + `python/av_cli/docker/docker-compose.release.yml` — single
  service/container `aether-vault-engine`, ports 8000+3000, `AV_ENGINE_ROLE=all`,
  dual healthcheck (python-urllib :8000 && node fetch :3000). db/redis unchanged
  (dev compose keeps host mappings for tests; release compose does not).
- `.github/workflows/docker-edge.yml` + `release.yml` — ONE build/push step; engine
  image plus legacy aliases (`aether-vault-server/-webui` :edge/:latest/:tag).
- `.dockerignore` — webui/node_modules etc. excluded from context.
- `python/av_cli/docker_runtime.py` — `RELEASE_IMAGES` → engine only;
  `ensure_local_backend_running` defaults → aether-vault-engine.
- `cmd_auth._restart_server_for_token_change` → restarts `aether-vault-engine`.
- Skip-hint strings updated everywhere (`db redis aether-vault-engine`):
  tests/skipsummary.py, test_skipsummary.py, test_server.py; auth restart assertions in
  test_cli.py updated to engine.

### Gap 1 — Env snapshot/replay
- core.py: `env_snapshot_file/canonical_env_bytes/env_snapshot_id/load_env_snapshot`
  (canonical = sorted-keys JSON minus `captured_at`; determinism contract);
  `upload_commit_objects` now also uploads `.av/env_snapshot.json` as a CAS object under
  its canonical hash (normal object flow); `commit_staged` attaches
  `commit_data["env_snapshot_id"]`.
- cmd_env.py REWRITTEN: snapshot stores into local CAS + links active run state;
  `av replay <run-id|commit-hash|snapshot-id>` resolves via registry/local CAS
  (`fetch_snapshot_by_id`, `resolve_replay_target`, pure `render_recipe` for golden tests).
- cmd_run.start posts + persists env_snapshot_id when a snapshot exists;
  server push_commit back-fills `runs.env_snapshot_id` on first linked commit.
- handoff.py: `.avh.replay.snapshot_id` added (additive).

### Gap 3 — Audit depth (server side)
- models/migration **0003**: `audit_log.status_code`, `commits.signature`,
  `webhook_deliveries` table (+4 indexes).
- `_audit(..., status_code=)` populated at every mutation site (201/200 outcomes).
- `GET /api/admin/audit`: action/project/since/until filters + limit+offset + total
  (`_parse_iso_dt` → 422 on bad input). `DELETE /api/admin/audit?before_days=N` prune.
- GC sweeps audit rows (`AV_AUDIT_RETENTION_DAYS`, default 90) + terminal webhook
  deliveries (event retention window).
- CLI `av audit list [--action --project --since --until --limit --offset]`
  (new module cmd_audit.py, registered in main.py after webhooks).

### Gap 4 — Signed commits (client + server storage)
- `python/av_cli/signing.py` NEW: ed25519 keygen (.av/keys/signing.pem 0600 +
  .pub), canonical bytes (sorted-keys JSON minus signature), sign_payload (best-effort,
  never raises), verify_signature ((ok, reason) tuples).
- pyproject extras: `sign = ["cryptography>=42.0.0"]`. cryptography 46 IS installed in
  the local dev venv → signing tests will run locally, not skip.
- `_finalize_commit` auto-signs AFTER hash computation (signature covers hash), before
  persist. Server persists signature blob (commits.signature) and echoes it in
  GET /api/commits/{hash} + list (`_signature_out`) so clones verify too.
- `av registry keygen` → ed25519 keypair (refuses overwrite; fails cleanly without
  [sign]). `av verify <hash>` → signature-first, legacy HMAC attest fallback,
  honest UNSIGNED verdict (exit 0).

### Gap 6 — Plugin seam migration
- `core.commit_scoped_paths(repo_root, paths, message, tags=(), metrics=None,
  run_id=None)` — THE machine-commit seam: direct stage_one_file staging (no CLI hop,
  no chdir), baseline-preserving scope (#38/#71), missing-path skip (#76), AV_RUN_ID
  resolution via cmd_run.current_run_id.
- `_shared.commit_scoped` now delegates (NEW signature message/tags/metrics);
  lightning/transformers/mlflow all converted; `run_av` kept ONLY for push flush.
- tests/test_plugins.py call sites updated to new signature; framework-free subset
  green (14 passed).

### Gap 7 — smaller items
- Perf gate 3×→2× (`tests/test_perf_gate.py` rewritten); speedcheck gained
  `semdiff.diff_trees()` probe (500-entry synthetic tree pair, budget 100ms).
- Webhook delivery persistence: `DBWebhookDelivery` + `_deliver_one/_deliver_webhooks(db,…)`
  (rows ride the mutation's transaction), `process_due_webhook_deliveries()`,
  startup+interval retry worker in lifespan (cancel-safe),
  `AV_WEBHOOK_MAX_ATTEMPTS`(5)/`AV_WEBHOOK_RETRY_INTERVAL_SECS`(30), dead-letter,
  `GET /api/admin/webhook-deliveries?status&webhook_id&limit&offset`.
- Divergence message: remote tip line UNCHANGED first (e2e Phase A parses it), then
  per-tip run attribution lines via `_tip_run_id`.

### Real bugs found & fixed (→ Probleme.md entries TODO)
1. **core.fail(None, …) raised AttributeError after printing the error** — every
   None-ctx caller (cmd_run/cmd_env/cmd_registry/cmd_policy paths) showed a traceback
   under clean validation failures and lost the documented exit code. Fixed:
   `ctx.exit()` when ctx present else `SystemExit(exit_code)`.
2. **cmd_registry.restore referenced undefined `ctx_exit`** — latent NameError on any
   failed restore. Fixed with module-local helper.
3. **Legacy-adoption gap**: adopting a TRUE pre-Alembic volume stamped the whole chain
   WITHOUT creating post-create_all tables (v1.2.0 runs/events/webhooks/audit would
   silently never exist there). Fixed: `_create_missing_tables()` from Base.metadata in
   database.py adoption path + `_LEGACY_COLUMNS` extended (commits.signature,
   audit_log.status_code).

## 2. REMAINING (exact resume order)

1. **Gap 5 — WebUI Run detail panel** (NOT started): RunsPanel expandable row → detail
   (parent lineage walk, linked commits w/ messages, metrics table, client-side semantic
   summary from last two commits' trees — NO new server endpoint; live badge already in
   card header, wire into TopBar if cheap). New pure lib `webui/src/lib/runDetail.ts`
   (lineageChain + tree-diff summary) + Vitest tests. Then vitest/tsc/lint.
2. **tests/test_signing.py** (roundtrip / tamper / unsigned-ok; importorskip cryptography;
   golden canonical-bytes fixture; verify CLI via CliRunner incl. legacy attest fallback).
3. **Dataset CDC boundary-stability tests parametrized across ALL CHUNKABLE_EXTS**
   (native-core skipif; mid-file flip ⇒ reused>0, reassembly byte-identical) +
   **.avattributes enforcement matrix** (no-chunk/no-layer-split across every new ext) —
   new tests/test_dataset_cdc.py or extend test_cli/test_core.
4. **dedup_efficiency → .avh flow test** (build_semantic_summary carries the key).
5. **Live server tests** (test_server.py): audit outcome capture + identity attribution,
   retention sweep, filters; webhook delivery row lifecycle + dead-letter (drive
   process_due_webhook_deliveries directly); signature round-trip over the wire;
   run env_snapshot_id back-fill.
6. **Parity tests** (test_plugins or test_v122): plugin seam vs SDK vs CLI commit parity;
   AV_RUN_ID + metrics flow through commit_scoped_paths.
7. **Unit**: policy/audit filter validation, schema-file validation path
   (av_cli/schemas/avh-2.0.json against golden doc; jsonschema optional).
8. **CI (tests.yml)**: plugin-tests job gains `sign` extra; NEW `e2e-engine-smoke` job =
   Phase I (docker build engine image, start role=all, health :8000+:3000, then
   role=server and role=webui dispatch checks); packaging smoke jobs gain
   `av diff --help` + `av registry --help` lines.
9. **scripts/e2e_scenario.sh**: Phase J (sign roundtrip: keygen→commit→verify→tamper;
   guard on cryptography presence) + Phase K (audit query with filters live).
10. **Verification battery**: full pytest · vitest/tsc/lint · e2e script local (embedded PG;
    phases now A–K) · eager-annotation checker · YAML parse all workflows · link sweep ·
    git status clean-tree review.
11. **Docs**: README (engine install/diagram/GHCR lines, av audit/replay/keygen/verify CLI
    ref, diagram command list) · architecture.md (Release Contract rewrite, Signing
    Contract, Webhook Delivery Contract, Audit expansion, Env Snapshot contract, testing
    map rows, module map additions signing.py/cmd_audit.py) · infrastructure.md (single-
    container stack list, new env vars, migration 0003, CI job map) · VERSIONING (alias
    deprecation = additive notes) · SECURITY (signing trust model section) ·
    docs/for-agents.md (events section unchanged; signature field note ok) · sub-READMEs
    (python/, av_cli/, av_server/, av_plugins/, webui/, scripts/, docker/ mention) ·
    CHANGELOG **Phase 56** · Probleme entries for the 3 real finds above ·
    development/README table (CONTINUATION file) · this file's status header → COMPLETE.
12. **Wrap-up per Essential-Tasks.md**: manual scratch-repo debug session (init→snapshot→
    commit w/ signing→push against embedded PG→clone→replay run id→verify tamper) ·
    HANDOFF.MD entry · vault regen (cd Aether-vault-Obsidian-Vault FIRST; both scripts
    with --append-handoff) · final chat report incl. DEFERRALS:
    **benchmark #5 re-run deferred (needs Docker session)** + anything else discovered.

## Known watch-items
- Perf gate at 2× flaked ONCE locally on `Storage stats`/disk-heavy probes during the
  pause-session full run (passed on rerun). CI runners are consistent; do NOT re-widen
  to 3× (plan says 2×) — if it flakes in CI, investigate the probe, not the multiplier.
- e2e Phase C asserts head `0003` now (updated); test_migrations heads/walk updated;
  test_server version assertions updated.
- Legacy alias semantics rely on entrypoint auto-detect — engine-smoke job must cover
  role=server and role=webui explicitly.
