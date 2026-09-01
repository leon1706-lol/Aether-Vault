# av_plugins

Owns the optional framework-native callbacks that stage and commit checkpoints
automatically during training, plus symmetric import commands for backfilling
artifacts that already exist. Installed via extras:
`pip install aether-vault[lightning]`, `[transformers]`, `[mlflow]`.

- `lightning.py` - `AetherVaultCallback` for PyTorch Lightning + `import_checkpoint()`.
- `transformers.py` - `AetherVaultTrainerCallback` for HuggingFace Transformers +
  `import_checkpoint()`.
- `mlflow.py` - `import_run()` - pulls artifacts and metrics from an MLflow server.
- `_shared.py` - the seam every plugin is built on: `commit_scoped()` delegates to
  `av_cli.core.commit_scoped_paths()` (direct staging + single-writer commit, no CLI
  hop, no chdir); `push_pending()` (v1.2.5) delegates to `av_cli.core.flush_pending_push()`
  the same way, for the training-end flush. `run_av()`/`build_metric_args()` are
  deprecated shims kept for one release's grace window (VERSIONING.md) — no plugin in
  this package calls either anymore; every framework's staging AND push are zero-chdir,
  zero-CLI-hop as of v1.2.5.

## Contracts

- Each framework imports lazily inside its module - installing `aether-vault` never
  pays for torch unless a callback actually runs; a missing extra raises a clear
  `ImportError` pointing at the right `pip install`.
- Machine commits are SCOPED: only the callback/import's own paths land; unrelated
  human-staged files keep their pending state, unchanged re-imports stay
  "Nothing to commit" no-ops, missing paths are skipped silently (Lightning fires
  `on_save_checkpoint` before writing - the next save picks stragglers up).
- AV_RUN_ID flows through `av_cli.core.resolve_run_id()` (v1.2.5: explicit arg > env >
  `.av/run.json` state — the single precedence rule shared with `av commit`/`av watch`)
  exactly like CLI commits; parity between seam, SDK, and CLI payload shape/schema, run
  linkage, env_snapshot_id, queued semantics, and error codes is pinned by
  `tests/test_plugins.py`'s "seam migration" section.

## Adding a new framework plugin

A new integration is a new module in this package plus a lazy-import guard plus a call
into the existing seam — it should never need to touch `av_cli/core.py` or duplicate any
staging/commit/push logic. Using `transformers.py` as the template:

1. **New module, `python/av_plugins/yourframework.py`.** Import your framework lazily
   inside functions/methods (never at module top level) so `import av_plugins` doesn't
   pay for a heavy dependency nobody asked for:
   ```python
   def _import_yourframework():
       try:
           import yourframework  # noqa: F401
       except ImportError as exc:
           raise ImportError(
               "AetherVaultYourFrameworkCallback requires yourframework. "
               "Install it with `pip install aether-vault[yourframework]`."
           ) from exc
   ```
2. **A callback/hook class** that calls `_shared.resolve_repo_root()` once (from wherever
   your framework hands you an output path — never `Path.cwd()`, see the contracts
   above) and `_shared.commit_scoped(repo_root, paths, message, tags=..., metrics=...)`
   on each save event — `commit_scoped` IS the single writer; do not call `av_cli.core`
   functions directly or shell out to the CLI.
3. **A training-end flush**: call `_shared.push_pending(repo_root)` from whatever
   end-of-training hook your framework offers (mirrors `lightning.py`/`transformers.py`'s
   `on_train_end`). Do not use `run_av()` — it's deprecated.
4. **A symmetric `import_*()` backfill function** for artifacts that already exist
   outside a live training run (mirrors `mlflow.py::import_run()` or the
   `import_checkpoint()` functions in `lightning.py`/`transformers.py`) — same
   `commit_scoped()` call, driven by whatever your framework's own artifact/run listing
   API returns.
5. **Register the extra** in `pyproject.toml` (`[project.optional-dependencies]`) as
   `yourframework = ["yourframework>=X.Y"]`, and add a CLI-level `av import-yourframework`
   command in `python/av_cli/main.py` mirroring the existing `import-lightning` /
   `import-transformers` / `import-mlflow` commands if a backfill CLI entry point makes
   sense for it.
6. **Tests**: add to `tests/test_plugins.py` — a real-callback test using the actual
   library (matches the file's existing pattern, `pytest.importorskip`-guarded so it
   skips cleanly without the extra installed), an import-error-message test (extra
   genuinely absent), and extend the "seam migration" parity section's loop-style
   assertions if your callback introduces a new field.
7. **CI**: add the new extra to the `plugin-tests` job's install line in
   `.github/workflows/tests.yml` so the real-callback test actually runs (not just skips)
   in CI.

Nothing above should require a new commit/push code path — if you find yourself reaching
for `av_cli.core.commit_staged`/`_finalize_commit`/`flush_pending_push` directly instead
of `_shared.commit_scoped`/`push_pending`, that's a sign the seam is missing something;
extend `_shared.py`, don't bypass it.
