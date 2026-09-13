"""`python/av_cli/launcher.py` -- the real console-script entry point. `_try_daemon`'s
fallback/dispatch logic. `first_subcommand()` itself (the fix for a real bug: `av --output
json status` never took the daemon path at all before, since `argv[0]` was `--output`, not
`status`) now lives in `daemon_common.py` -- see `tests/test_daemon.py` for its own direct
coverage, including the follow-up bug this session's native-launcher work found (`call_daemon`
and the server's `handle_request` each had their own separate, uncorrected positional check).
The native C++ launcher (a separate executable) is out of scope here -- this file is the
pure-Python fast path every install has today.
"""
import pytest

from python.av_cli import launcher


# ---------------------------------------------------------------------------
# _try_daemon -- fallback behavior when nothing is running / not allowlisted / disabled.
# All of these must return None (never raise), so main() always falls back in-process.
# ---------------------------------------------------------------------------

def test_try_daemon_returns_none_when_av_no_daemon_set(monkeypatch, tmp_path):
    monkeypatch.setenv("AV_NO_DAEMON", "1")
    assert launcher._try_daemon(["status"]) is None


def test_try_daemon_returns_none_for_non_allowlisted_command(monkeypatch):
    monkeypatch.delenv("AV_NO_DAEMON", raising=False)
    assert launcher._try_daemon(["login"]) is None


def test_try_daemon_returns_none_with_global_options_for_non_allowlisted_command(monkeypatch):
    monkeypatch.delenv("AV_NO_DAEMON", raising=False)
    assert launcher._try_daemon(["--output", "json", "webui"]) is None


def test_try_daemon_honors_global_options_for_an_allowlisted_command(monkeypatch, tmp_path):
    """Regression test for the real bug: previously `_try_daemon` (and `main()`) checked
    `argv[0]` directly, so `--output json status` was never even attempted against the
    daemon -- it always silently fell through to the in-process path regardless of whether
    a daemon was running. This drives it far enough to prove the allowlist check itself now
    correctly resolves "status" from `--output json status`, by making `_repo_root_or_none`
    return None right after (so the rest of the function returns None too, without needing
    a real daemon) and confirming we got PAST the allowlist gate to do so."""
    monkeypatch.delenv("AV_NO_DAEMON", raising=False)
    monkeypatch.chdir(tmp_path)  # no .av here -- _repo_root_or_none() returns None
    calls = []
    monkeypatch.setattr(launcher, "_repo_root_or_none", lambda: calls.append(1) or None)
    assert launcher._try_daemon(["--output", "json", "status"]) is None
    assert calls, "the allowlist check must have resolved 'status' and proceeded to look up a repo root"


def test_try_daemon_returns_none_outside_a_repo(monkeypatch, tmp_path):
    monkeypatch.delenv("AV_NO_DAEMON", raising=False)
    monkeypatch.chdir(tmp_path)
    assert launcher._try_daemon(["status"]) is None


# ---------------------------------------------------------------------------
# main() -- the top-level dispatch. Never actually spawns a daemon or falls all the way
# through to `av_cli.main.run()` in these tests (that would require a real CLI invocation);
# instead verifies the daemon-attempt gate itself fires (or doesn't) for the right argv
# shapes by monkeypatching `_try_daemon` and stubbing the in-process fallback.
# ---------------------------------------------------------------------------

def test_main_attempts_daemon_for_bare_allowlisted_command(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_try_daemon", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(launcher.sys, "argv", ["av", "status"])
    with pytest.raises(SystemExit) as ei:
        launcher.main()
    assert ei.value.code == 0
    assert calls == [["status"]]


def test_main_attempts_daemon_when_global_options_precede_the_command(monkeypatch):
    """The actual regression: `av --output json status` must reach `_try_daemon` at all --
    before the fix, `main()`'s own gate (`argv[0] in (...)`) rejected this shape before
    `_try_daemon` was ever called."""
    calls = []
    monkeypatch.setattr(launcher, "_try_daemon", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(launcher.sys, "argv", ["av", "--output", "json", "status"])
    with pytest.raises(SystemExit) as ei:
        launcher.main()
    assert ei.value.code == 0
    assert calls == [["--output", "json", "status"]]


def test_main_never_attempts_daemon_for_help(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_try_daemon", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(launcher.sys, "argv", ["av", "status", "--help"])
    monkeypatch.setattr(launcher, "run", lambda: None, raising=False)
    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module, "run", lambda: None)
    launcher.main()
    assert calls == []


def test_main_falls_back_in_process_when_daemon_unavailable(monkeypatch):
    monkeypatch.setattr(launcher, "_try_daemon", lambda argv: None)
    monkeypatch.setattr(launcher.sys, "argv", ["av", "status"])
    ran = []
    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module, "run", lambda: ran.append(1))
    launcher.main()
    assert ran == [1]


def test_main_never_raises_when_try_daemon_itself_errors(monkeypatch):
    def _boom(argv):
        raise RuntimeError("daemon path exploded")

    monkeypatch.setattr(launcher, "_try_daemon", _boom)
    monkeypatch.setattr(launcher.sys, "argv", ["av", "status"])
    ran = []
    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module, "run", lambda: ran.append(1))
    launcher.main()  # must not propagate the RuntimeError
    assert ran == [1]
