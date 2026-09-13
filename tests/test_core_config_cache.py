"""`load_config()`/`save_config()`'s V1.6.0 mtime+size cache (core.py). A single `av
commit`/`av add` invocation calls `load_config()` 2-3 times against the same unchanged
file; this cache turns the repeats into a stat-only hit. Every test here is about proving
the cache can never serve stale data -- speed with no observable behavior change is the
whole point, so "does this ever return the wrong thing" is what actually matters.
"""
import json

from python.av_cli.core import load_config, save_config, _config_cache


def test_second_load_is_a_cache_hit_and_returns_equal_but_independent_dict(repo):
    first = load_config(repo)
    second = load_config(repo)
    assert first == second
    # Independent objects: mutating one must never affect the other, or a later
    # unrelated load_config() call for the same repo could see a caller's local edit that
    # was never actually saved to disk.
    second["lfs_threshold_mb"] = 999999
    third = load_config(repo)
    assert third["lfs_threshold_mb"] != 999999


def test_external_write_via_save_config_is_seen_on_next_load(repo):
    cfg = load_config(repo)
    cfg["lfs_threshold_mb"] = 7
    save_config(repo, cfg)
    reloaded = load_config(repo)
    assert reloaded["lfs_threshold_mb"] == 7


def test_direct_file_write_bypassing_save_config_still_invalidates_the_cache(repo):
    """Many tests (and `av config`) write `.av/config` directly rather than through
    save_config() -- the cache must key off the file's own mtime/size, not assume it's the
    only writer."""
    load_config(repo)  # populate the cache
    config_path = repo / ".av" / "config"
    data = json.loads(config_path.read_text())
    data["lfs_threshold_mb"] = 3
    config_path.write_text(json.dumps(data))
    reloaded = load_config(repo)
    assert reloaded["lfs_threshold_mb"] == 3


def test_cache_miss_after_external_write_even_when_size_is_unchanged(repo):
    """A same-length value change (7 -> 3, both single digits) must still invalidate --
    proves the check isn't accidentally only sensitive to size."""
    cfg = load_config(repo)
    cfg["lfs_threshold_mb"] = 7
    save_config(repo, cfg)
    config_path = repo / ".av" / "config"
    data = json.loads(config_path.read_text())
    assert data["lfs_threshold_mb"] == 7
    data["lfs_threshold_mb"] = 3  # same digit count -> same byte length
    config_path.write_text(json.dumps(data))
    assert len(json.dumps(data)) == len(json.dumps({**data, "lfs_threshold_mb": 7}))
    reloaded = load_config(repo)
    assert reloaded["lfs_threshold_mb"] == 3


def test_two_repos_never_share_a_cache_entry(repo, tmp_path):
    other = tmp_path / "other-repo"
    (other / ".av").mkdir(parents=True)
    save_config(other, {"lfs_threshold_mb": 50, "project_id": "other-id", "project_name": "other"})

    cfg_repo = load_config(repo)
    cfg_other = load_config(other)
    assert cfg_repo["project_id"] != cfg_other["project_id"]

    cfg_repo["lfs_threshold_mb"] = 42
    save_config(repo, cfg_repo)
    assert load_config(other)["lfs_threshold_mb"] != 42


def test_missing_config_file_is_never_cached_as_a_false_positive(tmp_path):
    """load_config() on a repo with no config file at all returns synthesized defaults
    every time -- these must never accidentally populate `_config_cache` under a key that
    later collides with a real file created at the same path."""
    (tmp_path / ".av").mkdir()
    before = len(_config_cache)
    result = load_config(tmp_path)
    assert result["lfs_threshold_mb"] == 50
    assert len(_config_cache) == before  # no cache entry created for a nonexistent file

    # Now a real config file appears at the same path -- must be read fresh, not confused
    # with the synthesized defaults from the call above.
    save_config(tmp_path, {"lfs_threshold_mb": 11, "project_id": "x", "project_name": "y"})
    assert load_config(tmp_path)["lfs_threshold_mb"] == 11
