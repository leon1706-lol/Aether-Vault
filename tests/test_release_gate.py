"""v1.3.0 (todo.md item 30): scripts/release_gate.py's checks — each read-only, unit-
tested independently of the GitHub API / a real release, per the script's own module
docstring on why it's factored out of release.yml this way. The GitHub API check itself
(check_tagged_commit_tests_green) needs a real network call and isn't unit-tested here;
it's exercised for real by the `gate` job in CI.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "release_gate.py"
_spec = importlib.util.spec_from_file_location("release_gate", _SCRIPT_PATH)
rg = importlib.util.module_from_spec(_spec)
sys.modules["release_gate"] = rg
_spec.loader.exec_module(rg)


# ---------------------------------------------------------------------------
# check_perf_history_has_tag
# ---------------------------------------------------------------------------

def test_perf_history_check_passes_when_the_version_is_present(tmp_path):
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "perf-history.json").write_text(
        '{"schema": "perf-history-1.0", "entries": [{"version": "1.3.0", "probes": {}}]}',
        encoding="utf-8",
    )
    ok, detail = rg.check_perf_history_has_tag(tmp_path, "v1.3.0")
    assert ok, detail


def test_perf_history_check_fails_when_the_version_is_missing(tmp_path):
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "perf-history.json").write_text(
        '{"schema": "perf-history-1.0", "entries": [{"version": "1.2.5", "probes": {}}]}',
        encoding="utf-8",
    )
    ok, detail = rg.check_perf_history_has_tag(tmp_path, "v1.3.0")
    assert not ok
    assert "1.3.0" in detail


def test_perf_history_check_fails_cleanly_when_the_file_is_missing(tmp_path):
    ok, detail = rg.check_perf_history_has_tag(tmp_path, "v1.3.0")
    assert not ok
    assert "does not exist" in detail


def test_perf_history_check_fails_cleanly_on_malformed_json(tmp_path):
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "perf-history.json").write_text("{not json", encoding="utf-8")
    ok, detail = rg.check_perf_history_has_tag(tmp_path, "v1.3.0")
    assert not ok
    assert "not valid JSON" in detail


# ---------------------------------------------------------------------------
# check_changelog_has_signed_off_entry
# ---------------------------------------------------------------------------

def test_changelog_check_passes_when_the_latest_entry_is_signed_off(tmp_path):
    # This project APPENDS new entries at the BOTTOM of the file (verified against the
    # real development/CHANGELOG.md, whose Phase 1 is the first header and Phase 57 the
    # last) — so the fixture below puts the newest phase LAST, matching real practice.
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## Phase 57 — v1.2.5\n\nOlder entry, no marker needed here.\n\n"
        "## Phase 58 — v1.3.0\n\nDid stuff.\n\nEssential-Tasks: signed off\n",
        encoding="utf-8",
    )
    ok, detail = rg.check_changelog_has_signed_off_entry(tmp_path)
    assert ok, detail


def test_changelog_check_fails_when_the_latest_entry_has_no_marker(tmp_path):
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Phase 58 — v1.3.0\n\nForgot the marker.\n",
        encoding="utf-8",
    )
    ok, detail = rg.check_changelog_has_signed_off_entry(tmp_path)
    assert not ok
    assert "Phase 58" in detail


def test_changelog_check_ignores_a_marker_in_an_older_entry(tmp_path):
    # Only the LATEST (last-appended) entry counts — an old entry's marker doesn't
    # grandfather in a new, un-signed-off release.
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Phase 57 — v1.2.5\n\nEssential-Tasks: signed off\n\n"
        "## Phase 58 — v1.3.0\n\nNo marker here.\n",
        encoding="utf-8",
    )
    ok, detail = rg.check_changelog_has_signed_off_entry(tmp_path)
    assert not ok


def test_changelog_check_fails_cleanly_when_the_file_has_no_headers(tmp_path):
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "CHANGELOG.md").write_text("no headers at all", encoding="utf-8")
    ok, detail = rg.check_changelog_has_signed_off_entry(tmp_path)
    assert not ok
    assert "no '## Phase N' entries" in detail


# ---------------------------------------------------------------------------
# check_benchmarks_captured_sha_is_an_ancestor (needs a real git repo)
# ---------------------------------------------------------------------------

def _git(repo, *args):
    import os

    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


def _init_repo_with_two_commits(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    first_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                               capture_output=True, text=True, check=True).stdout.strip()
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    _git(tmp_path, "add", "b.txt")
    _git(tmp_path, "commit", "-q", "-m", "second")
    _git(tmp_path, "tag", "v1.3.0")
    return first_sha


def test_benchmarks_check_passes_when_the_captured_sha_is_an_ancestor_of_the_tag(tmp_path):
    first_sha = _init_repo_with_two_commits(tmp_path)
    (tmp_path / "development").mkdir(exist_ok=True)
    (tmp_path / "development" / "BENCHMARKS.md").write_text(
        f"**Captured:** 2026-09-02, on Linux. Aether-Vault @ `{first_sha[:7]}`, git-lfs 3.7.1.\n",
        encoding="utf-8",
    )
    ok, detail = rg.check_benchmarks_captured_sha_is_an_ancestor(tmp_path, "v1.3.0")
    assert ok, detail


def test_benchmarks_check_fails_when_the_captured_sha_is_not_a_real_ancestor(tmp_path):
    _init_repo_with_two_commits(tmp_path)
    (tmp_path / "development").mkdir(exist_ok=True)
    (tmp_path / "development" / "BENCHMARKS.md").write_text(
        "**Captured:** 2026-01-01, on Linux. Aether-Vault @ `deadbee`, git-lfs 3.7.1.\n",
        encoding="utf-8",
    )
    ok, detail = rg.check_benchmarks_captured_sha_is_an_ancestor(tmp_path, "v1.3.0")
    assert not ok


def test_benchmarks_check_fails_cleanly_when_no_captured_line_exists(tmp_path):
    _init_repo_with_two_commits(tmp_path)
    (tmp_path / "development").mkdir(exist_ok=True)
    (tmp_path / "development" / "BENCHMARKS.md").write_text("# nothing here\n", encoding="utf-8")
    ok, detail = rg.check_benchmarks_captured_sha_is_an_ancestor(tmp_path, "v1.3.0")
    assert not ok
    assert "no '**Captured:**" in detail
