"""Vanilla PyTorch checkpointer: auto-versions checkpoints for training loops that use
no framework (Lightning/Transformers) at all.

Usage:
    from av_plugins.pytorch import AetherVaultCheckpointer

    with AetherVaultCheckpointer("checkpoints/", tag="experiment-1") as ckpt:
        for epoch in range(n_epochs):
            train_one_epoch(...)
            ckpt.save(model, epoch=epoch, optimizer=optimizer,
                      metrics={"val_loss": val_loss})
"""
import itertools
from pathlib import Path

from ._shared import commit_scoped, push_pending, resolve_repo_root

try:
    import torch
except ImportError as exc:
    raise ImportError(
        "AetherVaultCheckpointer requires PyTorch. "
        "Install it with `pip install aether-vault[pytorch]`."
    ) from exc

AV_FORMAT = 1  # bump on any incompatible change to the payload shape below.


def _numeric_metrics(metrics: dict | None) -> dict:
    """Unwraps 0-dim tensors to Python scalars and drops anything non-numeric — the same
    filter every other plugin applies before metrics ride a commit."""
    if not metrics:
        return {}
    unwrapped = {k: (v.item() if hasattr(v, "item") else v) for k, v in metrics.items()}
    return {k: v for k, v in unwrapped.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


class AetherVaultCheckpointer:
    """Saves a checkpoint to disk AND versions it in one call. Vanilla PyTorch has no
    callback system to hook, so unlike the Lightning/Transformers plugins this is an
    object the training loop calls explicitly rather than a framework-injected callback.

    `dataset_paths`, if given, is committed once (on the first `save()`, or via an
    explicit `start()`), tagged `dataset` so `av handoff`'s extension-based classification
    picks it up as dataset lineage rather than a model checkpoint — mirrors
    `lightning.py`'s `on_train_start` semantics.
    """

    def __init__(
        self,
        checkpoint_dir: str | Path,
        tag: str | None = None,
        dataset_paths: str | list[str] | None = None,
        filename_template: str = "epoch{epoch}.pt",
        repo_root: Path | None = None,
    ):
        if isinstance(dataset_paths, str):
            dataset_paths = [dataset_paths]
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.tag = tag
        self.dataset_paths = dataset_paths
        self.filename_template = filename_template
        self.repo_root = repo_root if repo_root is not None else resolve_repo_root(self.checkpoint_dir)
        self._started = False
        self._save_counter = itertools.count()

    def start(self) -> None:
        """Commits `dataset_paths` once, tagged `dataset`. Idempotent — safe to call more
        than once, and `save()` calls it automatically so a caller who forgets still gets
        dataset lineage."""
        if self._started:
            return
        self._started = True
        if not self.dataset_paths:
            return
        # Scoped: a dataset commit must not sweep unrelated staged files into the tree.
        commit_scoped(self.repo_root, list(self.dataset_paths),
                      f"datasets for {self.tag or 'run'}", tags=("dataset",))

    def save(
        self,
        model=None,
        *,
        epoch: int | None = None,
        step: int | None = None,
        metrics: dict | None = None,
        optimizer=None,
        scheduler=None,
        extra: dict | None = None,
        path: str | Path | None = None,
    ) -> str | None:
        """Writes a `torch.save` checkpoint and commits it. Returns the new commit hash,
        or `None` when nothing changed (re-saving identical content is a no-op, matching
        every other plugin's re-import behavior)."""
        self.start()

        if path is not None:
            checkpoint_path = Path(path)
        else:
            save_index = next(self._save_counter)
            checkpoint_path = self.checkpoint_dir / self.filename_template.format(
                epoch=epoch if epoch is not None else save_index,
                step=step if step is not None else 0,
            )

        numeric_metrics = _numeric_metrics(metrics)

        payload: dict = {"av_format": AV_FORMAT}
        if model is not None:
            payload["model"] = model.state_dict()
        if optimizer is not None:
            payload["optimizer"] = optimizer.state_dict()
        if scheduler is not None:
            payload["scheduler"] = scheduler.state_dict()
        if epoch is not None:
            payload["epoch"] = epoch
        if step is not None:
            payload["step"] = step
        if numeric_metrics:
            payload["metrics"] = numeric_metrics
        if extra:
            payload.update(extra)

        torch.save(payload, checkpoint_path)

        # Only the parts actually given -- unlike Lightning/Transformers, a vanilla loop
        # very often tracks epoch alone, and a literal "step=None" reads as a bug.
        parts = []
        if epoch is not None:
            parts.append(f"epoch={epoch}")
        if step is not None:
            parts.append(f"step={step}")
        message = " ".join(parts) if parts else checkpoint_path.name
        tags = (self.tag,) if self.tag else ()
        return commit_scoped(self.repo_root, [str(checkpoint_path)], message,
                              tags=tags, metrics=numeric_metrics)

    def finish(self) -> dict:
        """Drains `.av/pending_push` — the training-end flush every plugin calls."""
        return push_pending(self.repo_root)

    def __enter__(self) -> "AetherVaultCheckpointer":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.finish()


def load_checkpoint(
    checkpoint_path: str | Path,
    model=None,
    optimizer=None,
    scheduler=None,
    map_location: str = "cpu",
) -> dict:
    """Restores `state_dict`s into whichever of `model`/`optimizer`/`scheduler` is given
    and returns the raw payload (so the caller can read `epoch`/`step`/`metrics`).

    torch >= 2.6 flipped `torch.load`'s `weights_only` default to True; our own payload
    is safe under that, but a caller's `extra=` may hold arbitrary picklables, so this
    tries the safe default first and only falls back to `weights_only=False` if that
    rejects the file.
    """
    try:
        payload = torch.load(checkpoint_path, map_location=map_location, weights_only=True)
    except Exception:
        payload = torch.load(checkpoint_path, map_location=map_location, weights_only=False)

    if model is not None and "model" in payload:
        model.load_state_dict(payload["model"])
    if optimizer is not None and "optimizer" in payload:
        optimizer.load_state_dict(payload["optimizer"])
    if scheduler is not None and "scheduler" in payload:
        scheduler.load_state_dict(payload["scheduler"])
    return payload


def latest_checkpoint(checkpoint_dir: str | Path) -> str | None:
    """Returns the newest `*.pt` file in `checkpoint_dir` by mtime, or `None` if empty."""
    candidates = list(Path(checkpoint_dir).glob("*.pt"))
    if not candidates:
        return None
    return str(max(candidates, key=lambda p: p.stat().st_mtime))


def import_checkpoint(
    checkpoint_path: str,
    repo_root: Path | None = None,
    tag: str | None = None,
    metrics: dict | None = None,
) -> None:
    """Backfills a pre-existing PyTorch checkpoint that wasn't captured live by the
    checkpointer.

    If `metrics` isn't given, attempts to read them from the checkpoint file itself (our
    own `"metrics"` key first, then `"callback_metrics"` for a foreign checkpoint written
    by another tool) -- best-effort, since checkpoint internals vary.
    """
    checkpoint_path = str(checkpoint_path)
    resolved_root = repo_root if repo_root is not None else resolve_repo_root(Path(checkpoint_path).parent)

    if metrics is None:
        metrics = {}
        try:
            loaded = load_checkpoint(checkpoint_path)
            raw_metrics = loaded.get("metrics") or loaded.get("callback_metrics", {})
            metrics = _numeric_metrics(raw_metrics) if isinstance(raw_metrics, dict) else {}
        except Exception:
            metrics = {}

    tags = ("pytorch-import",)
    if tag:
        tags += (tag,)
    commit_scoped(resolved_root, [checkpoint_path],
                  f"Imported PyTorch checkpoint {Path(checkpoint_path).name}",
                  tags=tags, metrics=metrics)
