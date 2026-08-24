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


def commit_scoped(repo_root: Path, paths: list[str], commit_args: list[str]) -> None:
    """Stages `paths` and commits ONLY them, leaving unrelated staged work alone.

    Fixes Probleme.md #38: `av commit` snapshots the whole index, so an import firing
    while the user had unrelated files staged used to sweep them into the import's
    commit under the import's message/tags. Since the tree IS the full index, isolation
    means scoping the staging area for exactly one commit — WITHOUT destroying the
    change-detection baseline (Probleme.md #71): `add` must still see the real entries
    for the target paths, or a re-import of unchanged content looks "new" and produces
    a duplicate commit instead of the intended "Nothing to commit" no-op.

    Sequence: snapshot everything → run the real CLI `add` against the untouched index
    → reload and scope the index to exactly the keys `add` touched (new keys or newly
    staged) → run the normal single-code-path commit → merge every other entry back in
    `finally` with its staged flag untouched. A plain `av commit` keeps full-snapshot
    semantics; only machine-driven plugin events are scoped. Still drives add/commit
    through the CLI (same in-process invocation as `run_av`) — zero duplicated commit
    logic; only the staging scope is managed here.
    """
    previous_cwd = Path.cwd()
    os.chdir(repo_root)
    import copy

    from av_cli.index import Index

    idx = Index(repo_root)
    saved = copy.deepcopy(idx.entries)
    baseline_keys = set(saved)
    # Staged-before-import set: lets the scoping step below tell "add staged this" apart
    # from "the USER had this staged long before the import fired" — both read as
    # staged=True afterwards.
    pre_staged = {rel_path for rel_path, entry in saved.items() if entry.get("staged")}
    try:
        cli.main(args=["add", *paths], prog_name="av", standalone_mode=False)

        # Scope to exactly what THIS add touched: brand-new keys, keys whose content
        # changed under a known path (re-staged by add), and keys that transitioned into
        # staged by this add. Unchanged re-imports touch nothing → scoped index stays
        # empty → the commit is the documented no-op rather than a duplicate.
        post_add = Index(repo_root)
        post_add.entries = {
            rel_path: entry
            for rel_path, entry in post_add.entries.items()
            if rel_path not in baseline_keys
            or entry.get("hash") != saved[rel_path].get("hash")
            or (entry.get("staged") and rel_path not in pre_staged)
        }
        post_add.save()

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
