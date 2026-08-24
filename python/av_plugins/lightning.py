"""PyTorch Lightning callback: auto-versions checkpoints with Aether-Vault.

Usage:
    from av_plugins.lightning import AetherVaultCallback
    trainer = Trainer(callbacks=[AetherVaultCallback(tag="experiment-1")])
"""
from pathlib import Path

from ._shared import build_metric_args, commit_scoped, filter_existing_files, resolve_repo_root, run_av

try:
    from lightning.pytorch.callbacks import Callback
except ImportError:
    try:
        from pytorch_lightning.callbacks import Callback
    except ImportError as exc:
        raise ImportError(
            "AetherVaultCallback requires PyTorch Lightning. "
            "Install it with `pip install aether-vault[lightning]`."
        ) from exc


class AetherVaultCallback(Callback):
    """Stages and commits Lightning checkpoints as they're saved.

    `checkpoint_paths`, if given, overrides which file(s) to stage instead of
    reading them from `trainer.checkpoint_callback`. Numeric entries in
    `trainer.callback_metrics` are recorded as commit metrics. `dataset_paths`,
    if given, is committed once at the start of training, tagged `dataset` so
    `av handoff`'s extension-based classification picks it up as dataset
    lineage rather than a model checkpoint.
    """

    def __init__(
        self,
        checkpoint_paths: str | list[str] | None = None,
        dataset_paths: str | list[str] | None = None,
        tag: str | None = None,
    ):
        if isinstance(checkpoint_paths, str):
            checkpoint_paths = [checkpoint_paths]
        if isinstance(dataset_paths, str):
            dataset_paths = [dataset_paths]
        self.checkpoint_paths = checkpoint_paths
        self.dataset_paths = dataset_paths
        self.tag = tag

    def _resolve_checkpoint_paths(self, trainer) -> list[str]:
        if self.checkpoint_paths:
            paths = self.checkpoint_paths
        else:
            paths = []
            ckpt_cb = getattr(trainer, "checkpoint_callback", None)
            for attr in ("best_model_path", "last_model_path"):
                path = getattr(ckpt_cb, attr, None) if ckpt_cb else None
                if path:
                    paths.append(path)
        return filter_existing_files(paths)

    def on_train_start(self, trainer, pl_module) -> None:
        if not self.dataset_paths:
            return
        repo_root = resolve_repo_root(Path(self.dataset_paths[0]).parent)
        commit_args = ["commit", "-m", f"datasets for {self.tag or 'run'}", "--tag", "dataset"]
        # Scoped: a dataset commit must not sweep unrelated staged files into the tree
        # (Probleme.md #38).
        commit_scoped(repo_root, list(self.dataset_paths), commit_args)

    def on_save_checkpoint(self, trainer, pl_module, checkpoint) -> None:
        paths = self._resolve_checkpoint_paths(trainer)
        if not paths:
            return

        repo_root = resolve_repo_root(Path(paths[0]).parent)

        metrics = {
            k: v.item() if hasattr(v, "item") else v
            for k, v in trainer.callback_metrics.items()
        }
        message = f"epoch={trainer.current_epoch} step={trainer.global_step}"
        commit_args = ["commit", "-m", message, *build_metric_args(metrics)]
        if self.tag:
            commit_args.extend(["--tag", self.tag])
        commit_scoped(repo_root, paths, commit_args)

    def on_train_end(self, trainer, pl_module) -> None:
        paths = self._resolve_checkpoint_paths(trainer)
        if not paths:
            return
        repo_root = resolve_repo_root(Path(paths[0]).parent)
        run_av(repo_root, ["push"])


def import_checkpoint(
    checkpoint_path: str,
    repo_root: Path | None = None,
    tag: str | None = None,
    metrics: dict | None = None,
) -> None:
    """Backfills a pre-existing Lightning checkpoint that wasn't captured live by the callback.

    If `metrics` isn't given, attempts to read `checkpoint["callback_metrics"]` from the
    `.ckpt` file itself (best-effort — checkpoint internals vary across Lightning versions).
    """
    checkpoint_path = str(checkpoint_path)
    resolved_root = repo_root if repo_root is not None else resolve_repo_root(Path(checkpoint_path).parent)

    if metrics is None:
        metrics = {}
        try:
            import torch

            loaded = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
            raw_metrics = loaded.get("callback_metrics", {}) if isinstance(loaded, dict) else {}
            metrics = {k: v.item() if hasattr(v, "item") else v for k, v in raw_metrics.items()}
        except Exception:
            metrics = {}

    commit_args = [
        "commit",
        "-m",
        f"Imported Lightning checkpoint {Path(checkpoint_path).name}",
        "--tag",
        "lightning-import",
        *build_metric_args(metrics),
    ]
    if tag:
        commit_args.extend(["--tag", tag])
    commit_scoped(resolved_root, [checkpoint_path], commit_args)
