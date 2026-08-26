# av_plugins

Owns the optional framework-native callbacks that stage and commit checkpoints
automatically during training, plus symmetric import commands for backfilling
artifacts that already exist. Installed via extras:
`pip install aether-vault[lightning]`, `[transformers]`, `[mlflow]`.

- `lightning.py` - `AetherVaultCallback` for PyTorch Lightning + `import_checkpoint()`.
- `transformers.py` - `AetherVaultTrainerCallback` for HuggingFace Transformers +
  `import_checkpoint()`.
- `mlflow.py` - `import_run()` - pulls artifacts and metrics from an MLflow server.
- `_shared.py` - v1.2.2 seam: `commit_scoped()` delegates to
  `av_cli.core.commit_scoped_paths()` - direct staging + single-writer commit, NO CLI
  hop, no chdir. `run_av()` remains only for the deliberate `push` flush at training
  end.

## Contracts

- Each framework imports lazily inside its module - installing `aether-vault` never
  pays for torch unless a callback actually runs; a missing extra raises a clear
  `ImportError` pointing at the right `pip install`.
- Machine commits are SCOPED: only the callback/import's own paths land; unrelated
  human-staged files keep their pending state, unchanged re-imports stay
  "Nothing to commit" no-ops, missing paths are skipped silently (Lightning fires
  `on_save_checkpoint` before writing - the next save picks stragglers up).
- AV_RUN_ID flows exactly like CLI commits; parity between seam, SDK, and CLI payload
  shape is pinned by `tests/test_plugins.py`.
