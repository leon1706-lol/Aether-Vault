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
| `_shared.py` | `run_av()`: invokes the CLI **in-process** (temporary chdir + `cli.main(standalone_mode=False)`) so plugins reuse every guarantee of `av add/commit/push` without duplicating logic |

## Design notes

- Each framework is a lazy import inside the module — the core package stays
  framework-agnostic, and a missing extra raises a clear `ImportError` pointing at the
  right `pip install`.
- Commits made by callbacks behave exactly like manual ones: same LFS pointers,
  safetensors layer-splitting / CDC chunking, offline pending-push queueing, project
  namespacing.
- Caveat (intentional): an import/callback commits *everything currently staged*, matching
  `git`-like semantics of `av commit` everywhere else.
