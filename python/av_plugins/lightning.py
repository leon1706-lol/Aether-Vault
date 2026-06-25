"""PyTorch Lightning callback: auto-versions checkpoints with Aether-Vault.

Usage:
    from av_plugins.lightning import AetherVaultCallback
    trainer = Trainer(callbacks=[AetherVaultCallback(tag="experiment-1")])
"""
from pathlib import Path

from ._shared import build_metric_args, resolve_repo_root, run_av

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
    `trainer.callback_metrics` are recorded as commit metrics.
    """

    def __init__(self, checkpoint_paths: str | list[str] | None = None, tag: str | None = None):
        if isinstance(checkpoint_paths, str):
            checkpoint_paths = [checkpoint_paths]
        self.checkpoint_paths = checkpoint_paths
        self.tag = tag

    def _resolve_checkpoint_paths(self, trainer) -> list[str]:
        if self.checkpoint_paths:
            return self.checkpoint_paths

        paths = []
        ckpt_cb = getattr(trainer, "checkpoint_callback", None)
        for attr in ("best_model_path", "last_model_path"):
            path = getattr(ckpt_cb, attr, None) if ckpt_cb else None
            if path:
                paths.append(path)
        return paths

    def on_save_checkpoint(self, trainer, pl_module, checkpoint) -> None:
        paths = self._resolve_checkpoint_paths(trainer)
        if not paths:
            return

        repo_root = resolve_repo_root(Path(paths[0]).parent)
        run_av(repo_root, ["add", *paths])

        metrics = {
            k: v.item() if hasattr(v, "item") else v
            for k, v in trainer.callback_metrics.items()
        }
        message = f"epoch={trainer.current_epoch} step={trainer.global_step}"
        commit_args = ["commit", "-m", message, *build_metric_args(metrics)]
        if self.tag:
            commit_args.extend(["--tag", self.tag])
        run_av(repo_root, commit_args)

    def on_train_end(self, trainer, pl_module) -> None:
        paths = self._resolve_checkpoint_paths(trainer)
        if not paths:
            return
        repo_root = resolve_repo_root(Path(paths[0]).parent)
        run_av(repo_root, ["push"])
