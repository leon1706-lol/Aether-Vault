"""HuggingFace Transformers callback: auto-versions Trainer checkpoints.

Usage:
    from av_plugins.transformers import AetherVaultTrainerCallback
    trainer = Trainer(..., callbacks=[AetherVaultTrainerCallback(tag="run-1")])
"""
import json
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
    records them as commit metrics. `dataset_paths`, if given, is committed
    once at the start of training, tagged `dataset` so `av handoff`'s
    extension-based classification picks it up as dataset lineage rather
    than a model checkpoint.
    """

    def __init__(self, dataset_paths: str | list[str] | None = None, tag: str | None = None):
        if isinstance(dataset_paths, str):
            dataset_paths = [dataset_paths]
        self.dataset_paths = dataset_paths
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

    def on_train_begin(self, args, state, control, **kwargs) -> None:
        if not self.dataset_paths:
            return
        repo_root = resolve_repo_root(Path(self.dataset_paths[0]).parent)
        run_av(repo_root, ["add", *self.dataset_paths])
        run_av(repo_root, ["commit", "-m", f"datasets for {self.tag or 'run'}", "--tag", "dataset"])

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


def import_checkpoint(checkpoint_dir: str, repo_root: Path | None = None, tag: str | None = None) -> None:
    """Backfills a pre-existing HF Trainer checkpoint directory not captured live by the callback.

    Metrics are read from the checkpoint's own `trainer_state.json` (`log_history`) when present.
    """
    checkpoint_dir = str(checkpoint_dir)
    resolved_root = repo_root if repo_root is not None else resolve_repo_root(Path(checkpoint_dir))

    metrics = {}
    state_path = Path(checkpoint_dir) / "trainer_state.json"
    if state_path.is_file():
        try:
            log_history = json.loads(state_path.read_text()).get("log_history", [])
            for entry in reversed(log_history):
                numeric = {k: v for k, v in entry.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
                if numeric:
                    metrics = numeric
                    break
        except Exception:
            metrics = {}

    run_av(resolved_root, ["add", checkpoint_dir])
    commit_args = [
        "commit",
        "-m",
        f"Imported Transformers checkpoint {Path(checkpoint_dir).name}",
        "--tag",
        "transformers-import",
        *build_metric_args(metrics),
    ]
    if tag:
        commit_args.extend(["--tag", tag])
    run_av(resolved_root, commit_args)
