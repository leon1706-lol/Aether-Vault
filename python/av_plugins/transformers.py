"""HuggingFace Transformers callback: auto-versions Trainer checkpoints.

Usage:
    from av_plugins.transformers import AetherVaultTrainerCallback
    trainer = Trainer(..., callbacks=[AetherVaultTrainerCallback(tag="run-1")])
"""
from pathlib import Path

from ._shared import build_metric_args, resolve_repo_root, run_av

try:
    from transformers import TrainerCallback
except ImportError as exc:
    raise ImportError(
        "AetherVaultTrainerCallback requires HuggingFace Transformers. "
        "Install it with `pip install aether-vault[transformers]`."
    ) from exc


class AetherVaultTrainerCallback(TrainerCallback):
    """Stages and commits HF `Trainer` checkpoints as they're saved.

    Reads the most recent numeric metrics from `state.log_history` and
    records them as commit metrics.
    """

    def __init__(self, tag: str | None = None):
        self.tag = tag

    @staticmethod
    def _checkpoint_dir(args, state) -> str:
        return f"{args.output_dir.rstrip('/')}/checkpoint-{state.global_step}"

    @staticmethod
    def _latest_numeric_metrics(state) -> dict:
        for entry in reversed(state.log_history):
            numeric = {k: v for k, v in entry.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
            if numeric:
                return numeric
        return {}

    def on_save(self, args, state, control, **kwargs) -> None:
        ckpt_dir = self._checkpoint_dir(args, state)
        if not Path(ckpt_dir).is_dir():
            return

        repo_root = resolve_repo_root(Path(ckpt_dir))
        run_av(repo_root, ["add", ckpt_dir])

        metrics = self._latest_numeric_metrics(state)
        commit_args = ["commit", "-m", f"step={state.global_step}", *build_metric_args(metrics)]
        if self.tag:
            commit_args.extend(["--tag", self.tag])
        run_av(repo_root, commit_args)

    def on_train_end(self, args, state, control, **kwargs) -> None:
        repo_root = resolve_repo_root(Path(args.output_dir))
        run_av(repo_root, ["push"])
