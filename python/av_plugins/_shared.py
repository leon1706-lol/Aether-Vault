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
