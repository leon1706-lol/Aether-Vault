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


def _run_av(args: list[str], cwd) -> subprocess.CompletedProcess:
    """Runs one `av` invocation as a real, fresh subprocess (own interpreter, own
    sys.modules) via `av_cli.main.run()` -- `AV_NO_DAEMON=1` so a real `av status`/`add`
    can't be served by an auto-spawned background daemon instead (which would make the
    checking process's own sys.modules reflect only the tiny daemon-client transport, not
    the in-process import graph these tests exist to guard). encoding/errors explicit, not
    text=True: `av init`'s banner prints non-ASCII box-drawing characters the default
    locale codepage (cp1252 on this dev box) can't decode."""
    code = (
        "import os\n"
        "os.environ['AV_NO_DAEMON'] = '1'\n"
        "import sys\n"
        f"sys.argv = {args!r}\n"
        "from av_cli.main import run\n"
        "try:\n"
        "    run()\n"
        "except SystemExit:\n"
        "    pass\n"
        "heavy = ('tempfile', 'urllib.parse', 'concurrent.futures', 'shutil', 'subprocess', 'uuid', 'datetime')\n"
        "print('HEAVY_LOADED=' + ','.join(sorted(m for m in heavy if m in sys.modules)))\n"
    )
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=60)


def _heavy_loaded(result: subprocess.CompletedProcess) -> list[str]:
    for line in result.stdout.splitlines():
        if line.startswith("HEAVY_LOADED="):
            rest = line[len("HEAVY_LOADED="):]
            return rest.split(",") if rest else []
    raise AssertionError(f"marker line missing -- process failed?\n{result.stderr}")


# V1.6.0 (WS2.1): `core.py`'s own module scope no longer imports `datetime`/`shutil`/
# `subprocess`/`tempfile`/`uuid`/`concurrent.futures.ThreadPoolExecutor` -- `tempfile` and
# `urllib.parse` had ZERO internal use in core.py even before this change (pure re-export
# dead weight for other modules); the rest are genuinely used, just deferred to their
# actual point of use (a real commit, a real upload, a restore, a one-time config backfill,
# `av init`, or a genuinely multi-file threaded `add`), since core.py is reached by every
# command via `from .core import *`. `urllib.parse`/`concurrent.futures` are the only two
# of those names an end-to-end `status`/no-op-`add` run can actually be asserted empty on
# today, though: `main.py` itself still imports `datetime`/`shutil`/`subprocess`/
# `tempfile`/`uuid` at module scope regardless of anything core.py does (its own
# top-of-file comment explains why -- removing them broke `test_cli.py`'s patch-anchor
# dependencies on `main_module.subprocess`/`main_module.shutil` specifically, a real,
# already-diagnosed constraint from earlier in this same phase, not an oversight here).
_ACHIEVABLE_HEAVY = ("urllib.parse", "concurrent.futures")


def test_status_in_a_real_repo_does_not_load_heavy_stdlib_modules(tmp_path):
    """`av init` itself runs in its OWN separate subprocess here, deliberately not
    measured -- it legitimately touches more (uuid for project_id, and more besides) and
    isn't part of this phase's `status`/no-op-`add` speed claim; conflating its cost with
    `status`'s in one process previously made this test fail for a reason that had nothing
    to do with `status` itself.

    `AV_NO_DAEMON=1` (via `_run_av`) is required -- otherwise a real `av status` could be
    served by an auto-spawned background daemon, and the checking process's own
    `sys.modules` would reflect only the tiny daemon-client transport, not the in-process
    import graph this test actually exists to guard."""
    init_result = _run_av(["av", "init", "--mode", "local", "--yes", "--no-repl"], tmp_path)
    assert init_result.returncode == 0, init_result.stderr

    status_result = _run_av(["av", "status"], tmp_path)
    assert status_result.returncode == 0, status_result.stderr
    loaded = [m for m in _heavy_loaded(status_result) if m in _ACHIEVABLE_HEAVY]
    assert loaded == []


def test_noop_add_does_not_load_heavy_stdlib_modules(tmp_path):
    """Counterpart to the status test above for `add .` of an already-staged, unchanged
    file -- the other half of the "no-op" hot path this phase's benchmark work targets. The
    first `add` (a genuinely new file) runs in its own subprocess and legitimately needs
    uuid/shutil (see core.py's own comments on those call sites) -- only the SECOND,
    unchanged-file `add`, in a fresh process of its own, is the actual no-op under test."""
    init_result = _run_av(["av", "init", "--mode", "local", "--yes", "--no-repl"], tmp_path)
    assert init_result.returncode == 0, init_result.stderr
    (tmp_path / "a.py").write_text("print('hi')\n")
    first_add = _run_av(["av", "add", "."], tmp_path)
    assert first_add.returncode == 0, first_add.stderr

    noop_add = _run_av(["av", "add", "."], tmp_path)
    assert noop_add.returncode == 0, noop_add.stderr
    loaded = [m for m in _heavy_loaded(noop_add) if m in _ACHIEVABLE_HEAVY]
    assert loaded == []


def test_list_commands_matches_full_expected_surface_without_importing_anything():
    expected = sorted([
        "add", "add-user", "admin", "audit", "auth", "benchmark", "blackboard", "branch",
        "budget", "canary", "checkout", "clone", "commit", "config", "context", "critique",
        "daemon", "diff", "doctor", "env", "eval", "fetch", "file", "freeze", "gc", "graph", "handoff", "idp",
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
