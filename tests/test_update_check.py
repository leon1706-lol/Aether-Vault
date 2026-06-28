import time

import pytest

from python.av_cli import update_check


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path, monkeypatch):
    monkeypatch.setattr(update_check, "USER_CONFIG_DIR", tmp_path / ".aether-vault")
    monkeypatch.setattr(update_check, "USER_CONFIG_PATH", tmp_path / ".aether-vault" / "config.json")


def test_cache_hit_skips_network_call(monkeypatch):
    calls = []
    monkeypatch.setattr(update_check, "_fetch_latest_version", lambda *a, **k: calls.append(1) or "9.9.9")

    cfg = update_check.load_user_config()
    cfg["last_check_ts"] = time.time()
    cfg["last_check_result"] = {"checked_version": "1.0.0", "latest_version": "1.2.0"}
    update_check.save_user_config(cfg)

    result = update_check.check_for_update(cache_hours=12)
    assert result is not None
    assert result.latest == "1.2.0"
    assert result.is_outdated is True
    assert calls == []  # cache hit: no network call made


def test_network_failure_does_not_raise_and_does_not_poison_cache(monkeypatch):
    monkeypatch.setattr(update_check, "_fetch_latest_version", lambda *a, **k: None)
    result = update_check.check_for_update(force=True)
    assert result is None

    cfg = update_check.load_user_config()
    assert cfg["last_check_result"] is None  # cache untouched on failure


def test_version_comparison_outdated():
    assert update_check._is_outdated("1.0.0", "1.2.0") is True
    assert update_check._is_outdated("1.2.0", "1.2.0") is False
    assert update_check._is_outdated("1.10.0", "1.2.0") is False  # real semver compare


def test_update_check_disabled_skips_network(monkeypatch):
    calls = []
    monkeypatch.setattr(update_check, "_fetch_latest_version", lambda *a, **k: calls.append(1) or "9.9.9")
    cfg = update_check.load_user_config()
    cfg["update_check_enabled"] = False
    update_check.save_user_config(cfg)

    result = update_check.check_for_update()
    assert result is None
    assert calls == []


def test_list_versions_sorted_descending(monkeypatch):
    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"releases": {"1.0.0": [{}], "1.2.0": [{}], "0.9.0": [{}], "1.1.0": []}}

    monkeypatch.setattr(update_check.requests, "get", lambda *a, **k: _FakeResp())
    versions = update_check.list_versions()
    assert versions == ["1.2.0", "1.0.0", "0.9.0"]  # 1.1.0 excluded: empty file list (yanked)


# ---------------------------------------------------------------------------
# maybe_auto_update — opt-in silent upgrade, wired into main.run() at process exit
# ---------------------------------------------------------------------------

def _set_auto_update(enabled: bool) -> None:
    cfg = update_check.load_user_config()
    cfg["auto_update"] = enabled
    update_check.save_user_config(cfg)


def test_maybe_auto_update_noop_when_not_opted_in(monkeypatch):
    _set_auto_update(False)
    calls = []
    monkeypatch.setattr(update_check, "check_for_update", lambda *a, **k: calls.append(1))

    assert update_check.maybe_auto_update() is False
    assert calls == []  # never even checks for an update if not opted in


def test_maybe_auto_update_noop_when_already_up_to_date(monkeypatch):
    _set_auto_update(True)
    monkeypatch.setattr(
        update_check, "check_for_update",
        lambda *a, **k: update_check.UpdateCheckResult(current="1.0.0", latest="1.0.0", is_outdated=False),
    )

    def fake_run(*a, **k):
        raise AssertionError("should not invoke pip when already up to date")

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert update_check.maybe_auto_update() is False


def test_maybe_auto_update_upgrades_when_outdated(monkeypatch):
    _set_auto_update(True)
    monkeypatch.setattr(
        update_check, "check_for_update",
        lambda *a, **k: update_check.UpdateCheckResult(current="1.0.0", latest="2.0.0", is_outdated=True),
    )

    calls = []

    class _FakeCompletedProcess:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompletedProcess()

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert update_check.maybe_auto_update() is True
    assert calls and calls[0][-3:] == ["install", "--upgrade", "aether-vault"]


def test_maybe_auto_update_reports_failure_on_nonzero_pip_exit(monkeypatch):
    _set_auto_update(True)
    monkeypatch.setattr(
        update_check, "check_for_update",
        lambda *a, **k: update_check.UpdateCheckResult(current="1.0.0", latest="2.0.0", is_outdated=True),
    )

    class _FakeCompletedProcess:
        returncode = 1

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompletedProcess())

    assert update_check.maybe_auto_update() is False
