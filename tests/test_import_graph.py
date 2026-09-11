"""V1.5.0 perf work: `av_cli.main` used to eagerly import all ~45 `cmd_*` modules, and one
of them (`cmd_login` -> `session_store` -> `update_check`) pulled the entire `requests` /
`urllib3` / `ssl` stack into every single `av` invocation just to read one constant
(`USER_CONFIG_DIR`, now defined dependency-free in `fsutil.py`). These tests run in a fresh
subprocess (not the test-runner's own process, which has already imported all sorts of
things via other tests/plugins/conftest) and are mechanical drift-checks in the same spirit
as `scripts/check_eager_annotations.py`: they must keep failing loudly if a future change
reintroduces an eager heavy import on the `import av_cli.main` critical path, since that
path runs on literally every `av` command including a no-op `--version`/`--help`.
"""
import subprocess
import sys


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)


def test_import_main_does_not_load_requests():
    result = _run(
        "import sys\n"
        "import av_cli.main\n"
        "assert 'requests' not in sys.modules, sorted(m for m in sys.modules if 'requests' in m or 'urllib3' in m)\n"
    )
    assert result.returncode == 0, result.stderr


def test_import_main_does_not_load_speedcheck():
    # speedcheck.py is only needed by `doctor --speed`/`test --speed` -- pulls in
    # `statistics`/`subprocess` for no reason on every other command otherwise.
    result = _run(
        "import sys\n"
        "import av_cli.main\n"
        "assert 'av_cli.speedcheck' not in sys.modules\n"
    )
    assert result.returncode == 0, result.stderr


def test_import_main_does_not_load_any_cmd_module():
    # Command modules (cmd_repo, cmd_staging, cmd_history, ...) are registered lazily via
    # _AuthRetryGroup._LAZY_LOADERS (core.py) and must stay unimported until a matching
    # command name is actually resolved -- that's the whole point of the lazy registration.
    result = _run(
        "import sys\n"
        "import av_cli.main\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('av_cli.cmd_'))\n"
        "assert loaded == [], loaded\n"
    )
    assert result.returncode == 0, result.stderr


def test_get_command_lazily_imports_only_its_own_module():
    # Resolving `commit` must import cmd_history and nothing else command-module-shaped.
    result = _run(
        "import sys\n"
        "import av_cli.main as m\n"
        "m.cli.get_command(None, 'commit')\n"
        "loaded = sorted(mod for mod in sys.modules if mod.startswith('av_cli.cmd_'))\n"
        "assert loaded == ['av_cli.cmd_history'], loaded\n"
    )
    assert result.returncode == 0, result.stderr


def test_running_version_does_not_load_ui_questionary_rich_or_requests():
    # V1.5.0 perf fix (Probleme.md #150): `_AuthRetryGroup.invoke()` (core.py) used to
    # import `av_cli.ui` (and, via its module-level `import questionary`, the whole
    # questionary/rich stack -- ~1.3-1.4s measured) unconditionally at the top of every
    # single command dispatch, including a no-op `av --version`/`--help` -- silently
    # defeating this whole file's import-graph guarantees for the actually-invoked path,
    # not just the import path. `update_check.py`'s `requests` import was the same class of
    # bug one level further out (`run()`'s finally always does `from . import update_check`).
    # This test actually RUNS the CLI (not just imports the module) to catch a regression
    # like this one, which only manifests once `invoke()`/`run()` executes.
    result = _run(
        "import sys\n"
        "sys.argv = ['av', '--version']\n"
        "from av_cli.main import run\n"
        "try:\n"
        "    run()\n"
        "except SystemExit:\n"
        "    pass\n"
        "loaded = sorted(m for m in sys.modules if m in ('requests', 'rich', 'questionary', 'av_cli.ui') or m.startswith(('rich.', 'questionary.', 'requests.')))\n"
        "assert loaded == [], loaded\n"
    )
    assert result.returncode == 0, result.stderr


def test_list_commands_matches_full_expected_surface_without_importing_anything():
    expected = sorted([
        "add", "add-user", "admin", "audit", "auth", "benchmark", "blackboard", "branch",
        "budget", "canary", "checkout", "clone", "commit", "config", "context", "critique",
        "daemon", "diff", "doctor", "env", "eval", "file", "freeze", "gc", "graph", "handoff", "idp",
        "import-lightning", "import-mlflow", "import-pytorch", "import-transformers",
        "improver", "incident", "init", "lessons", "lineage", "list-meta", "list-users",
        "log", "login", "logout", "merge", "plan", "policy", "promote", "pull", "push",
        "registry", "remove-user", "replay", "replay-actions", "review", "role", "run",
        "sandbox", "scheduler", "scim", "search", "stash", "status", "strategy",
        "support-bundle", "task", "tenant", "test", "token", "tools", "unstage", "update",
        "user", "verify", "watch", "webhooks", "webui", "whoami",
    ])
    result = _run(
        "import sys\n"
        "import av_cli.main as m\n"
        "names = m.cli.list_commands(None)\n"
        "loaded = sorted(mod for mod in sys.modules if mod.startswith('av_cli.cmd_'))\n"
        "print(repr(names))\n"
        "assert loaded == [], loaded\n"
    )
    assert result.returncode == 0, result.stderr
    printed_names = eval(result.stdout.strip())
    assert printed_names == expected
