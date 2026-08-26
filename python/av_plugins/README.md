# `av_plugins` — Framework Auto-Commit Callbacks

Optional, framework-native callbacks that stage and commit checkpoints automatically
during training, plus symmetric import commands for backfilling artifacts that already
exist. Installed via extras: `pip install aether-vault[lightning]`, `[transformers]`,
`[mlflow]`. See the [main README](../../README.md).

## Contents

| File | Purpose |
|---|---|
| `lightning.py` | `AetherVaultCallback` for PyTorch Lightning + `import_checkpoint()` |
| `transformers.py` | `AetherVaultTrainerCallback` for HuggingFace Transformers + `import_checkpoint()` |
| `mlflow.py` | `import_run()` — pulls artifacts and metrics from an MLflow tracking server |
| `_shared.py` | v1.2.2 seam: `commit_scoped()` delegates to `core.commit_scoped_paths()` — direct staging + single-writer commit, NO CLI hop, no chdir; `run_av()` remains only for the deliberate `push` flush at training end |

## Design notes

- Each framework is a lazy import inside the module — the core package stays
  framework-agnostic, and a missing extra raises a clear `ImportError` pointing at the
  right `pip install`.
- Commits made by callbacks behave exactly like manual ones: same LFS pointers,
  safetensors layer-splitting / CDC chunking, offline pending-push queueing, project
  namespacing.
- Scoped machine commits (v1.1.9, seam v1.2.2): imports/callbacks commit ONLY their own
  paths — unrelated human-staged files keep their pending state, unchanged re-imports are
  "Nothing to commit" no-ops, and AV_RUN_ID flows in exactly like CLI commits.
- Parity: the seam produces commit payloads identical to the SDK and CLI surfaces
  (same tree/metrics/tags structure) — pinned by tests/test_plugins.py.
