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
    # Same idea for cmd_devtools' two symbols (`test`/`benchmark`'s home module, otherwise
    # only imported on first `av test`/`av benchmark` invocation via _load_devtools above) --
    # a caller reading `main.BENCHMARK_NAMES`/`main._update_readme_test_badge` directly still
    # gets the real value, on demand, without forcing cmd_devtools eager for every command.
    if name in ("BENCHMARK_NAMES", "_update_readme_test_badge"):
        from . import cmd_devtools

        return getattr(cmd_devtools, name)
    raise AttributeError(name)


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
        from .fsutil import get_version

        click.echo(f"av {get_version()}")
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
    from .cmd_repo import _reconnect_existing_repo

    _reconnect_existing_repo(repo_root, cfg)
    from . import repl

    repl.run_repl(repo_root, login_mode=cfg.get("login_mode", "local"))


# --- Command registration ---
#
# V1.5.0 perf work: every one of the ~45 command modules below used to be imported
# unconditionally right here, so even `av --version`/`--help` paid for every module's full
# dependency tree. Each module is now registered as one small loader against the exact
# command name(s) it produces; `_AuthRetryGroup.get_command` (core.py) imports+registers a
# module only the first time one of its names is actually resolved -- `av commit` now only
# ever imports `cmd_history`, never the other 44 modules. `--help`/completion need no import
# at all: `list_commands` is just this dict's keys, sorted (click.Group's own default
# `list_commands` is already alphabetical, so this preserves the exact prior `--help` order).
# `cli.add_command(...)` inside each loader is unchanged from the eager code it replaces --
# only WHEN it runs changed, never WHAT it registers.


def _load_repo(group: click.Group) -> None:
    from .cmd_repo import init, update

    group.add_command(init)
    group.add_command(update)


def _load_staging(group: click.Group) -> None:
    from .cmd_staging import add, config, file, status, unstage

    for cmd in (config, add, file, unstage, status):
        group.add_command(cmd)


def _load_history(group: click.Group) -> None:
    from .cmd_history import branch, checkout, commit, list_meta, log, push, stash

    for cmd in (commit, branch, checkout, log, stash, list_meta, push):
        group.add_command(cmd)


def _load_sync(group: click.Group) -> None:
    from .cmd_sync import clone, merge, pull

    for cmd in (clone, pull, merge):
        group.add_command(cmd)


def _load_auth(group: click.Group) -> None:
    from .cmd_auth import auth, auth_add_user, auth_list_users, auth_remove_user

    group.add_command(auth)
    # Top-level aliases: same click Command objects also live as subcommands of `auth`
    # itself (`av auth add-user` == `av add-user`).
    group.add_command(auth_add_user)
    group.add_command(auth_list_users)
    group.add_command(auth_remove_user)


def _load_token(group: click.Group) -> None:
    from .cmd_token import token

    group.add_command(token)


def _load_tenant(group: click.Group) -> None:
    from .cmd_tenant import tenant

    group.add_command(tenant)


def _load_user(group: click.Group) -> None:
    from .cmd_user import user

    group.add_command(user)


def _load_role(group: click.Group) -> None:
    from .cmd_role import role

    group.add_command(role)


def _load_login(group: click.Group) -> None:
    from .cmd_login import login, logout, whoami

    for cmd in (login, logout, whoami):
        group.add_command(cmd)


def _load_idp(group: click.Group) -> None:
    from .cmd_idp import idp

    group.add_command(idp)


def _load_scim(group: click.Group) -> None:
    from .cmd_scim import scim

    group.add_command(scim)


def _load_admin(group: click.Group) -> None:
    from .cmd_admin import admin

    group.add_command(admin)


def _load_support(group: click.Group) -> None:
    from .cmd_support import support_bundle

    group.add_command(support_bundle)


def _load_maintenance(group: click.Group) -> None:
    from .cmd_maintenance import doctor, gc

    group.add_command(doctor)
    group.add_command(gc)


def _load_devtools(group: click.Group) -> None:
    from .cmd_devtools import benchmark, test_cmd

    group.add_command(test_cmd)
    group.add_command(benchmark)


def _load_integrations(group: click.Group) -> None:
    from .cmd_integrations import (
        graph,
        handoff,
        import_lightning,
        import_mlflow,
        import_pytorch,
        import_transformers,
        webui_cmd,
    )

    for cmd in (graph, handoff, webui_cmd, import_lightning, import_transformers,
                import_mlflow, import_pytorch):
        group.add_command(cmd)


def _load_diff(group: click.Group) -> None:
    from .cmd_diff import diff

    group.add_command(diff)


def _load_context(group: click.Group) -> None:
    from .cmd_context import context

    group.add_command(context)


def _load_run(group: click.Group) -> None:
    from .cmd_run import run

    group.add_command(run)


def _load_env(group: click.Group) -> None:
    from .cmd_env import env
    from .cmd_env import replay as replay_cmd

    group.add_command(env)
    # Top-level alias so agents can `av replay <run|commit|snapshot-id>` directly
    # (v1.2.2); `av env replay` remains the canonical home.
    group.add_command(replay_cmd)


def _load_policy(group: click.Group) -> None:
    from .cmd_policy import policy as policy_group
    from .cmd_policy import promote

    group.add_command(policy_group)
    group.add_command(promote)


def _load_watch(group: click.Group) -> None:
    from .cmd_watch import watch

    group.add_command(watch)


def _load_registry(group: click.Group) -> None:
    from .cmd_registry import registry
    from .cmd_registry import verify as registry_verify

    group.add_command(registry)
    # Top-level alias: docs have always told users to run `av verify <hash>`, so this
    # registers the same object under both names -- mirrors the `replay`/`env replay` pattern.
    group.add_command(registry_verify)


def _load_webhooks(group: click.Group) -> None:
    from .cmd_webhooks import webhooks

    group.add_command(webhooks)


def _load_audit(group: click.Group) -> None:
    from .cmd_audit import audit as audit_group

    group.add_command(audit_group)


def _load_improver(group: click.Group) -> None:
    from .cmd_improver import improver

    group.add_command(improver)


def _load_freeze(group: click.Group) -> None:
    from .cmd_freeze import freeze, incident

    group.add_command(freeze)
    group.add_command(incident)


def _load_canary(group: click.Group) -> None:
    from .cmd_canary import canary

    group.add_command(canary)


def _load_eval(group: click.Group) -> None:
    from .cmd_eval import eval_group

    group.add_command(eval_group)


def _load_task(group: click.Group) -> None:
    from .cmd_task import task

    group.add_command(task)


def _load_plan(group: click.Group) -> None:
    from .cmd_plan import plan

    group.add_command(plan)


def _load_budget(group: click.Group) -> None:
    from .cmd_budget import budget

    group.add_command(budget)


def _load_scheduler(group: click.Group) -> None:
    from .cmd_scheduler import scheduler

    group.add_command(scheduler)


def _load_review(group: click.Group) -> None:
    from .cmd_review import critique, review

    group.add_command(review)
    group.add_command(critique)


def _load_lineage(group: click.Group) -> None:
    from .cmd_lineage import lineage, search

    group.add_command(lineage)
    group.add_command(search)


def _load_strategy(group: click.Group) -> None:
    from .cmd_strategy import strategy

    group.add_command(strategy)


def _load_lessons(group: click.Group) -> None:
    from .cmd_lessons import lessons

    group.add_command(lessons)


def _load_blackboard(group: click.Group) -> None:
    from .cmd_blackboard import blackboard

    group.add_command(blackboard)


def _load_sandbox(group: click.Group) -> None:
    from .cmd_sandbox import replay_actions, sandbox

    group.add_command(sandbox)
    group.add_command(replay_actions)


def _load_tools(group: click.Group) -> None:
    from .cmd_tools import tools

    group.add_command(tools)


def _load_daemon(group: click.Group) -> None:
    from .cmd_daemon import daemon

    group.add_command(daemon)


_LOADERS_BY_NAMES: list[tuple[tuple[str, ...], object]] = [
    (("init", "update"), _load_repo),
    (("config", "add", "file", "unstage", "status"), _load_staging),
    (("commit", "branch", "checkout", "log", "stash", "list-meta", "push"), _load_history),
    (("clone", "pull", "merge"), _load_sync),
    (("auth", "add-user", "list-users", "remove-user"), _load_auth),
    (("token",), _load_token),
    (("tenant",), _load_tenant),
    (("user",), _load_user),
    (("role",), _load_role),
    (("login", "logout", "whoami"), _load_login),
    (("idp",), _load_idp),
    (("scim",), _load_scim),
    (("admin",), _load_admin),
    (("support-bundle",), _load_support),
    (("doctor", "gc"), _load_maintenance),
    (("test", "benchmark"), _load_devtools),
    (("graph", "handoff", "webui", "import-lightning", "import-transformers",
      "import-mlflow", "import-pytorch"), _load_integrations),
    (("diff",), _load_diff),
    (("context",), _load_context),
    (("run",), _load_run),
    (("env", "replay"), _load_env),
    (("policy", "promote"), _load_policy),
    (("watch",), _load_watch),
    (("registry", "verify"), _load_registry),
    (("webhooks",), _load_webhooks),
    (("audit",), _load_audit),
    (("improver",), _load_improver),
    (("freeze", "incident"), _load_freeze),
    (("canary",), _load_canary),
    (("eval",), _load_eval),
    (("task",), _load_task),
    (("plan",), _load_plan),
    (("budget",), _load_budget),
    (("scheduler",), _load_scheduler),
    (("review", "critique"), _load_review),
    (("lineage", "search"), _load_lineage),
    (("strategy",), _load_strategy),
    (("lessons",), _load_lessons),
    (("blackboard",), _load_blackboard),
    (("sandbox", "replay-actions"), _load_sandbox),
    (("tools",), _load_tools),
    (("daemon",), _load_daemon),
]

for _names, _loader in _LOADERS_BY_NAMES:
    for _name in _names:
        _AuthRetryGroup._LAZY_LOADERS[_name] = _loader
del _names, _loader


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
