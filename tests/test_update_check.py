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
