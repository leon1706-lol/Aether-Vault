"""Shared helpers for framework plugins (Lightning, Transformers, MLflow).

v1.2.2 migration: add/commit no longer shells through the CLI — plugins call
`core.commit_scoped_paths()` directly (the same internal seam av_sdk.Repo uses).

v1.2.5: `push` closes the last CLI hop too — `push_pending()` calls
`core.flush_pending_push()` directly (the exact same call `av_sdk.Repo.push()` makes),
so plugins now have ZERO remaining chdir/CLI-invocation assumptions. `run_av` and
`build_metric_args` were kept as thin deprecated shims for one release cycle
(VERSIONING.md's grace-window policy); that window closed at v1.3.0, the next MINOR
boundary, and both are now removed. `tests/test_plugins.py` keeps its own private
`_run_av()` helper (test infrastructure, not a public API) for the same "drive the real
CLI" convenience its parity tests still want.
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
    """v1.2.5: drains `.av/pending_push` via `core.flush_pending_push()` — the same call
    `av_sdk.Repo.push()` makes, no chdir, no CLI hop. Replaces `run_av(repo_root,
    ["push"])` as the training-end flush in every plugin callback.
    """
    from av_cli.client import VaultClient
    from av_cli.core import flush_pending_push, load_pending_push, resolve_remote

    client = VaultClient(*resolve_remote(repo_root))
    pending = load_pending_push(repo_root)
    if not pending:
        return {"drained": 0, "still_queued": 0}
    still = flush_pending_push(repo_root, client)
    return {"drained": len(pending) - len(still), "still_queued": len(still)}


def filter_existing_files(paths: list[str]) -> list[str]:
    """Keeps only paths that currently exist on disk.

    Lightning invokes on_save_checkpoint BEFORE the checkpoint file is written (the hook
    exists so callbacks can inject extras into the checkpoint dict), and
    ModelCheckpoint updates best/last_model_path around that same window — so a resolved
    path can legitimately not exist yet. Staging a missing file would abort the whole
    training loop with FileNotFoundError; skip it here and let the NEXT save event pick
    it up instead (Probleme.md #76). Lives in _shared so the regression test runs
    without framework extras installed.
    """
    return [p for p in paths if Path(p).is_file()]


def commit_scoped(
    repo_root: Path,
    paths: list[str],
    message: str,
    tags: tuple = (),
    metrics: dict | None = None,
) -> str | None:
    """Stages `paths` and commits ONLY them, leaving unrelated staged work alone.

    Thin delegation to `av_cli.core.commit_scoped_paths()` — THE shared machine-commit
    seam (also used by agent tooling). See that function's docstring for the scoping
    contract (Probleme.md #38 isolation, #71 baseline preservation, #76 missing-path
    tolerance) and AV_RUN_ID flow.

    Returns the commit hash, or None when nothing changed ("Nothing to commit" no-op).

    Backward-compat note: this used to take a ready-made `commit_args` CLI list; the
    signature now takes message/tags/metrics directly since v1.2.2 dropped the CLI hop.
    """
    from av_cli.core import commit_scoped_paths

    return commit_scoped_paths(repo_root, paths, message, tags=tuple(tags),
                               metrics=dict(metrics or {}))
