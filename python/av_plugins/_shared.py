"""Shared helpers for framework plugins (Lightning, Transformers, ...).

Plugins drive the existing `av` CLI in-process instead of duplicating its
add/commit/push logic (LFS pointers, safetensors layer splitting, pending-push
queueing, project-id namespacing). See ARCHITECTURE.md for the rationale.
"""
import os
from pathlib import Path

from av_cli.exceptions import AetherVaultException
from av_cli.main import cli


def resolve_repo_root(start: Path) -> Path:
    """Walks upward from `start` looking for a `.av` directory.

    Mirrors `find_repo_root()` in av_cli/main.py, but takes an explicit start
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

    `add`/`commit`/etc. resolve their repo via `Path.cwd()`, not an argument,
    so this temporarily chdirs into `repo_root` for the duration of the call.
    Exceptions propagate to the caller (standalone_mode=False disables click's
    own error printing + sys.exit) so training loops can decide how to react.
    """
    previous_cwd = Path.cwd()
    os.chdir(repo_root)
    try:
        cli.main(args=args, prog_name="av", standalone_mode=False)
    finally:
        os.chdir(previous_cwd)


def build_metric_args(metrics: dict) -> list[str]:
    """Converts a dict of numeric metrics into repeatable `--metric k=v` flags."""
    args = []
    for key, value in metrics.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            args.extend(["--metric", f"{key}={value}"])
    return args


def commit_scoped(repo_root: Path, paths: list[str], commit_args: list[str]) -> None:
    """Stages `paths` and commits ONLY them, leaving unrelated staged work alone.

    Fixes Probleme.md #38: `av commit` snapshots the whole index, so an import firing
    while the user had unrelated files staged used to sweep them into the import's
    commit under the import's message/tags. Since the tree IS the full index, isolation
    means scoping the staging area for exactly one commit: snapshot every entry, empty
    the index, let the real CLI re-add just the target paths and run the normal
    single-code-path commit, then merge everything else back with its staged flag
    untouched — so whatever the user had pending stays pending for their own next
    commit.

    Still drives the actual add/commit through the CLI (same in-process invocation as
    `run_av`) — zero duplicated commit logic; only the staging scope is managed here.
    Crash-safety: the pre-import snapshot is written atomically away in step 1 and the
    restore runs in `finally`, so every ordinary exception path (including `add`
    rejecting a bad path) leaves the user's staging area byte-identical.
    """
    import copy

    from av_cli.index import Index

    previous_cwd = Path.cwd()
    os.chdir(repo_root)
    idx = Index(repo_root)
    saved = copy.deepcopy(idx.entries)
    idx.entries = {}
    idx.save()
    try:
        cli.main(args=["add", *paths], prog_name="av", standalone_mode=False)
        cli.main(args=commit_args, prog_name="av", standalone_mode=False)
    finally:
        os.chdir(previous_cwd)
        # Post-commit index: the import's targets present with staged flags cleared by
        # _finalize_commit. Everything the user had staged before comes back unchanged.
        fresh = Index(repo_root)
        for rel_path, entry in saved.items():
            if rel_path not in fresh.entries:
                fresh.entries[rel_path] = entry
        fresh.save()
