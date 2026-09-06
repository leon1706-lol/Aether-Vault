"""Shared helpers for framework plugins (Lightning, Transformers, MLflow). Plugins call
`core.commit_scoped_paths()`/`core.flush_pending_push()` directly, the same internal
seams `av_sdk.Repo` uses -- no chdir, no CLI hop.
"""
from pathlib import Path

from av_cli.exceptions import AetherVaultException


def resolve_repo_root(start: Path) -> Path:
    """Walks upward from `start` looking for a `.av` directory.

    Mirrors `find_repo_root()` in av_cli/core.py, but takes an explicit start
    path so plugins work regardless of the training script's cwd.
    """
    start = start.resolve()
    for parent in [start] + list(start.parents):
        if (parent / ".av").is_dir():
            return parent
    raise AetherVaultException(
        f"Not an Aether-Vault repository (or any of the parent directories of {start})."
    )


def push_pending(repo_root: Path) -> dict:
    """Drains `.av/pending_push` via `core.flush_pending_push()` -- the training-end
    flush every plugin callback calls."""
    from av_cli.client import VaultClient
    from av_cli.core import flush_pending_push, load_pending_push, resolve_remote

    client = VaultClient(*resolve_remote(repo_root))
    pending = load_pending_push(repo_root)
    if not pending:
        return {"drained": 0, "still_queued": 0}
    still = flush_pending_push(repo_root, client)
    return {"drained": len(pending) - len(still), "still_queued": len(still)}


def filter_existing_files(paths: list[str]) -> list[str]:
    """Keeps only paths that currently exist on disk. Lightning invokes
    on_save_checkpoint BEFORE the checkpoint file is written, so a resolved path can
    legitimately not exist yet -- skip it here and let the next save event pick it up."""
    return [p for p in paths if Path(p).is_file()]


def commit_scoped(
    repo_root: Path,
    paths: list[str],
    message: str,
    tags: tuple = (),
    metrics: dict | None = None,
) -> str | None:
    """Stages `paths` and commits ONLY them, leaving unrelated staged work alone. Thin
    delegation to `av_cli.core.commit_scoped_paths()` -- see that function's docstring
    for the scoping contract. Returns the commit hash, or None when nothing changed."""
    from av_cli.core import commit_scoped_paths

    return commit_scoped_paths(repo_root, paths, message, tags=tuple(tags),
                               metrics=dict(metrics or {}))
