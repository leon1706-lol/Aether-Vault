import datetime
import fnmatch
import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from .client import VaultClient

_aether_core = None
_aether_core_load_attempted = False


def _get_aether_core():
    """Lazily import the aether_core pybind11 extension.

    Loading the compiled extension costs real time (~90ms) that no-op commands like a
    fully-cached `add` never recoup, since they never reach a hash/split call. Deferred to
    first actual use instead of importing unconditionally at module load.
    """
    global _aether_core, _aether_core_load_attempted
    if not _aether_core_load_attempted:
        try:
            import aether_core as _ac

            _aether_core = _ac
        except ImportError:
            _aether_core = None
        _aether_core_load_attempted = True
    return _aether_core


def __getattr__(name: str):
    # PEP 562 module __getattr__: keeps `av_cli.main.VaultClient` resolvable (tests and
    # other callers monkeypatch it via this attribute) without paying for `import requests`
    # at module load time for commands that never touch the network — see local
    # `from .client import VaultClient` imports inside the command functions that need it.
    if name == "VaultClient":
        from .client import VaultClient

        return VaultClient
    raise AttributeError


# --- Point-13 split: helpers live in core.py; commands live in cmd_*.py ---
# This module is the thin compat shell: it owns the cli group, the two
# monkeypatch-target functions below, registration ORDER (= av --help order),
# and the historical namespace surface tests/benchmarks import from here.


def _find_source_root() -> Path:
    """Locate the aether-vault source checkout this package was installed from.

    Only meaningful for an editable/dev install (`pip install -e .`); a wheel install has no
    `tests/` directory underneath it. Factored out as its own function (rather than inlined in
    `test_cmd`) so it can be monkeypatched independently in tests.
    """
    return Path(__file__).parents[2]


from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import _AuthRetryGroup  # noqa: F401


@click.group(invoke_without_command=True, cls=_AuthRetryGroup)
@click.option("--verbose", is_flag=True, default=False, help="Enable debug logging.")
@click.option("--silent", is_flag=True, default=False, help="Suppress all output.")
@click.option(
    "--output",
    "output_mode",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Agent-facing commands emit a stable JSON envelope instead of human text.",
)
@click.option(
    "--version",
    "show_version",
    is_flag=True,
    default=False,
    help="Print the installed version and exit (same source as the banner's corner).",
)
@click.pass_context
def cli(ctx: click.Context, verbose: bool, silent: bool, output_mode: str, show_version: bool) -> None:
    """Aether-Vault: High-performance version control for ML models & datasets."""
    ctx.ensure_object(dict)
    ctx.obj["output"] = output_mode
    from .core import set_output_mode

    set_output_mode(output_mode)
    setup_logging(verbose, silent)

    if show_version:
        from .ui import _get_version

        click.echo(f"av {_get_version()}")
        raise click.exceptions.Exit(0)

    if ctx.invoked_subcommand is not None:
        return

    # Bare `av` with no subcommand: in an already-initialized project, reconnect and drop
    # straight into the interactive session; otherwise fall back to the normal help screen.
    repo_root = find_repo_root()
    if repo_root is None:
        click.echo(ctx.get_help())
        click.echo("\nRun `av init` to get started.")
        return

    cfg = load_config(repo_root)
    _reconnect_existing_repo(repo_root, cfg)
    from . import repl

    repl.run_repl(repo_root, login_mode=cfg.get("login_mode", "local"))


# --- Command registration (order == av --help order) ---

from .cmd_repo import _reconnect_existing_repo, init, update  # noqa: F401  # noqa: E402
from .cmd_staging import add, config, file, status, unstage  # noqa: E402
from .cmd_history import (  # noqa: E402
    branch,
    checkout,
    commit,
    list_meta,
    log,
    push,
    stash,
)
from .cmd_sync import clone, merge, pull  # noqa: E402
from .cmd_auth import auth, auth_add_user, auth_list_users, auth_remove_user  # noqa: E402
from .cmd_token import token  # noqa: E402
from .cmd_tenant import tenant  # noqa: E402
from .cmd_user import user  # noqa: E402
from .cmd_role import role  # noqa: E402
from .cmd_login import login, logout, whoami  # noqa: E402
from .cmd_idp import idp  # noqa: E402
from .cmd_scim import scim  # noqa: E402
from .cmd_admin import admin  # noqa: E402
from .cmd_support import support_bundle  # noqa: E402
from .cmd_maintenance import doctor, gc  # noqa: E402
from .cmd_devtools import BENCHMARK_NAMES, benchmark, test_cmd  # noqa: E402
from .cmd_devtools import _update_readme_test_badge  # noqa: F401,E402
from .cmd_integrations import (  # noqa: E402
    graph,
    handoff,
    import_lightning,
    import_mlflow,
    import_transformers,
    webui_cmd,
)

cli.add_command(init)
cli.add_command(update)
cli.add_command(config)
cli.add_command(add)
cli.add_command(file)
cli.add_command(unstage)
cli.add_command(status)
cli.add_command(commit)
cli.add_command(branch)
cli.add_command(checkout)
cli.add_command(log)
from .cmd_diff import diff  # noqa: E402

cli.add_command(diff)
from .cmd_context import context  # noqa: E402

cli.add_command(context)
from .cmd_run import run  # noqa: E402

cli.add_command(run)
from .cmd_env import env  # noqa: E402

cli.add_command(env)
from .cmd_env import replay as replay_cmd  # noqa: E402

# Top-level alias so agents can `av replay <run|commit|snapshot-id>` directly
# (v1.2.2); `av env replay` remains the canonical home.
cli.add_command(replay_cmd)
from .cmd_policy import policy as policy_group, promote  # noqa: E402
from .cmd_watch import watch  # noqa: E402

cli.add_command(watch)
from .cmd_registry import registry, verify as registry_verify  # noqa: E402

cli.add_command(registry)
# Top-level alias: docs have always told users to run `av verify <hash>`, so this
# registers the same object under both names -- mirrors the `replay`/`env replay` pattern.
cli.add_command(registry_verify)
from .cmd_webhooks import webhooks  # noqa: E402

cli.add_command(webhooks)
from .cmd_audit import audit as audit_group  # noqa: E402

cli.add_command(audit_group)
from .cmd_improver import improver  # noqa: E402

cli.add_command(improver)
from .cmd_freeze import freeze, incident  # noqa: E402

cli.add_command(freeze)
cli.add_command(incident)
from .cmd_canary import canary  # noqa: E402

cli.add_command(canary)
from .cmd_eval import eval_group  # noqa: E402

cli.add_command(eval_group)
from .cmd_task import task  # noqa: E402

cli.add_command(task)
from .cmd_plan import plan  # noqa: E402

cli.add_command(plan)
from .cmd_budget import budget  # noqa: E402

cli.add_command(budget)
from .cmd_scheduler import scheduler  # noqa: E402

cli.add_command(scheduler)
from .cmd_review import critique, review  # noqa: E402

cli.add_command(review)
cli.add_command(critique)
from .cmd_lineage import lineage, search  # noqa: E402

cli.add_command(lineage)
cli.add_command(search)
from .cmd_strategy import strategy  # noqa: E402

cli.add_command(strategy)
from .cmd_lessons import lessons  # noqa: E402

cli.add_command(lessons)
from .cmd_blackboard import blackboard  # noqa: E402

cli.add_command(blackboard)
from .cmd_sandbox import replay_actions, sandbox  # noqa: E402

cli.add_command(sandbox)
cli.add_command(replay_actions)
from .cmd_tools import tools  # noqa: E402

cli.add_command(tools)

cli.add_command(policy_group)
cli.add_command(promote)
cli.add_command(clone)
cli.add_command(pull)
cli.add_command(merge)
cli.add_command(stash)
cli.add_command(list_meta)
cli.add_command(push)
cli.add_command(gc)
cli.add_command(auth)
cli.add_command(auth_add_user)
cli.add_command(auth_list_users)
cli.add_command(auth_remove_user)
cli.add_command(token)
cli.add_command(tenant)
cli.add_command(user)
cli.add_command(role)
cli.add_command(login)
cli.add_command(logout)
cli.add_command(whoami)
cli.add_command(idp)
cli.add_command(scim)
cli.add_command(admin)
cli.add_command(support_bundle)
cli.add_command(doctor)
cli.add_command(test_cmd)
cli.add_command(benchmark)
cli.add_command(graph)
cli.add_command(handoff)
cli.add_command(webui_cmd)
cli.add_command(import_lightning)
cli.add_command(import_transformers)
cli.add_command(import_mlflow)


# Historical namespace surface (tests/benchmarks import these from here):
from .core import (  # noqa: F401,E402
    flush_pending_push,
    iter_working_files,
    load_config,
    load_registry,
    queue_pending_push,
    save_config,
    update_registry,
    upload_commit_objects,
)


def run() -> None:
    """Console-script entry point. Wraps `cli()` so the opt-in auto-update check runs
    exactly once per OS process on exit -- including after any REPL session, which calls
    `cli.main()` once per line typed, not once per process. Any failure in the update
    check itself is swallowed so it can never mask the real command's exit code."""
    try:
        cli()
    finally:
        from . import update_check

        try:
            update_check.maybe_auto_update()
        except Exception:
            pass


if __name__ == "__main__":
    run()
