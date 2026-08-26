"""Shared helpers for framework plugins (Lightning, Transformers, MLflow).

v1.2.2 migration: add/commit no longer shells through the CLI — plugins call
`core.commit_scoped_paths()` directly (the same internal seam av_sdk.Repo uses), so
there is no chdir dance and exactly one commit writer for CLI, SDK, watch, AND
plugins. `run_av` remains ONLY for `push` (the deliberate CLI-flush at training end)
and is kept for backward compatibility with external callers.
"""
import os
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


def run_av(repo_root: Path, args: list[str]) -> None:
    """Invokes the `av` CLI in-process with `args`, as if run from `repo_root`.

    Kept for the plugin `push` flush (a CLI-flush by design — it drains the
    pending_push queue through the exact code an interactive user runs). `add`/`commit`
    no longer route here (see commit_scoped below).
    """
    previous_cwd = Path.cwd()
    os.chdir(repo_root)
    try:
        from av_cli.main import cli

        cli.main(args=args, prog_name="av", standalone_mode=False)
    finally:
        os.chdir(previous_cwd)


def build_metric_args(metrics: dict) -> list[str]:
    """Converts a dict of numeric metrics into repeatable `--metric k=v` flags.

    Retained for backward compatibility with external callers; since the v1.2.2 seam
    migration, plugins pass metric dicts straight to commit_scoped() instead."""
    args = []
    for key, value in metrics.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            args.extend(["--metric", f"{key}={value}"])
    return args


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
