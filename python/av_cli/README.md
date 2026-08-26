# av_cli

Owns the `av` command line interface and every client-side concern: repo init, the
staging index, the local commit DAG, content-addressed storage, remote sync, merges,
signing, diagnostics, and the benchmark tooling. Split so no file is a monolith:
shared logic lives in `core.py`, commands live one-feature-per `cmd_*.py`, and
`main.py` stays a thin compat shell.

- `main.py` - cli group construction + registration ORDER (= `av --help` order), the
  PEP 562 lazy `VaultClient`, the two monkeypatch-target owners
  (`_find_source_root`, `_update_readme_test_badge`), re-exports of the historical
  namespace surface.
- `core.py` - shared multi-consumer helpers: config/root/logging, staging
  (`stage_one_file`, avignore), restore machinery (`materialize_file`,
  `_materialize_tree`, `_collect_dirty_paths`), pending-push trio,
  `upload_commit_objects`, `_finalize_commit`, THE commit seam `commit_staged()`,
  env-snapshot identity helpers, JSON envelope + exit codes.
- command modules - `cmd_repo.py` (init/update), `cmd_staging.py`
  (config/add/file/unstage/status), `cmd_history.py`
  (commit/branch/checkout/log/stash/list-meta/push), `cmd_sync.py` (clone/pull/merge),
  `cmd_auth.py` (Protected-mode tokens + per-user management),
  `cmd_maintenance.py` (doctor/gc), `cmd_devtools.py` (test/benchmark/badge),
  `cmd_integrations.py` (graph/handoff/webui/plugin imports).
- agent-facing groups - `cmd_diff.py`, `cmd_context.py`, `cmd_run.py`, `cmd_env.py`
  (snapshot/replay incl. the top-level `av replay` alias), `cmd_policy.py`,
  `cmd_watch.py`, `cmd_registry.py` (export/restore/keygen/attest/verify),
  `cmd_webhooks.py`, `cmd_audit.py`.
- feature modules - `index.py` (`Index`), `merge.py` (pure algorithms), `sync.py`
  (clone/pull primitives), `history.py` (log walking/rendering), `attributes.py`
  (`.avattributes` directives), `client.py` (`VaultClient`), `pointer.py`,
  `fsutil.py`, `handoff.py`, `repl.py`, `docker_runtime.py`, `update_check.py`,
  `speedcheck.py`, `signing.py` (ed25519 commit signatures).

## Design invariants

- **Canonical hashing**: `hash_file_safe` always produces plain SHA-256; layer/chunk
  shards are additional objects, never replacements for content addressing.
- **One restore path**: checkout, clone, pull, and merge all go through
  `_materialize_tree()` - do not add another working-tree writer.
- **One commit writer**: CLI, SDK, watch, and plugins all funnel through
  `commit_staged()` -> `_finalize_commit()`; scoped machine commits ride
  `commit_scoped_paths()`. Never build a second persist path.
- **Offline resilience is sacred**: unreachable/401 pushes queue in
  `.av/pending_push` and drain later; nothing network-fatal ever loses a commit.
