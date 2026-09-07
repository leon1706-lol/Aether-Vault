"""v1.3.0 (todo.md item 30): scripts/release_gate.py's checks — each read-only, unit-
tested independently of the GitHub API / a real release, per the script's own module
docstring on why it's factored out of release.yml this way. check_required_checks_green
(v1.3.4 rename of check_tagged_commit_tests_green) still needs a real network call for
its actual GitHub query (_fetch_all_check_runs, exercised for real by the `gate` job in
CI, not unit-tested here) -- but its poll/retry/fail-fast decision logic
(_evaluate_required_checks, and the wait loop itself) takes injectable `fetch_fn`/
`sleep_fn` specifically so that logic CAN be unit-tested below without any network or
real waiting.
"""
import importlib.util
import subprocess
import sys
import time
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


# ---------------------------------------------------------------------------
# _parse_semver / _is_minor_or_above / _previous_tag (v1.3.4, W4a/b/c)
# ---------------------------------------------------------------------------

def test_parse_semver_extracts_major_minor_patch():
    assert rg._parse_semver("v1.3.4") == (1, 3, 4)
    assert rg._parse_semver("1.3.4") == (1, 3, 4)


def _init_repo_with_two_tags(tmp_path, first_tag, second_tag):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    _git(tmp_path, "tag", first_tag)
    first_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                               capture_output=True, text=True, check=True).stdout.strip()
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    _git(tmp_path, "add", "b.txt")
    _git(tmp_path, "commit", "-q", "-m", "second")
    _git(tmp_path, "tag", second_tag)
    return first_sha


def test_previous_tag_finds_the_tag_before(tmp_path):
    _init_repo_with_two_tags(tmp_path, "v1.3.3", "v1.3.4")
    assert rg._previous_tag(tmp_path, "v1.3.4") == "v1.3.3"


def test_previous_tag_is_none_for_the_first_tag_ever(tmp_path):
    _init_repo_with_two_tags(tmp_path, "v1.0.0", "v1.0.1")
    assert rg._previous_tag(tmp_path, "v1.0.0") is None


def test_is_minor_or_above_true_when_middle_component_changes(tmp_path):
    _init_repo_with_two_tags(tmp_path, "v1.3.3", "v1.4.0")
    assert rg._is_minor_or_above(tmp_path, "v1.4.0") is True


def test_is_minor_or_above_false_for_a_pure_patch_bump(tmp_path):
    _init_repo_with_two_tags(tmp_path, "v1.3.3", "v1.3.4")
    assert rg._is_minor_or_above(tmp_path, "v1.3.4") is False


# ---------------------------------------------------------------------------
# check_changelog_versioning_sync (todo.md item 21)
# ---------------------------------------------------------------------------

def _write_versioning(tmp_path, text):
    (tmp_path / "VERSIONING.md").write_text(text, encoding="utf-8")


def test_changelog_versioning_sync_passes_on_a_patch_release_with_no_versioning_section(tmp_path):
    _init_repo_with_two_tags(tmp_path, "v1.3.3", "v1.3.4")
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Phase 63 — v1.3.4\n\nBugfixes only.\n", encoding="utf-8")
    ok, detail = rg.check_changelog_versioning_sync(tmp_path, "v1.3.4")
    assert ok, detail


def test_changelog_versioning_sync_fails_when_latest_entry_doesnt_mention_the_tag(tmp_path):
    _init_repo_with_two_tags(tmp_path, "v1.3.3", "v1.3.4")
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Phase 63 — some other release\n\nStuff.\n", encoding="utf-8")
    ok, detail = rg.check_changelog_versioning_sync(tmp_path, "v1.3.4")
    assert not ok
    assert "does not mention" in detail


def test_changelog_versioning_sync_requires_a_versioning_section_on_minor(tmp_path):
    _init_repo_with_two_tags(tmp_path, "v1.3.3", "v1.4.0")
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Phase 63 — v1.4.0\n\nNew stuff.\n", encoding="utf-8")
    ok, detail = rg.check_changelog_versioning_sync(tmp_path, "v1.4.0")
    assert not ok
    assert "VERSIONING.md" in detail


def test_changelog_versioning_sync_passes_on_minor_with_a_real_section(tmp_path):
    _init_repo_with_two_tags(tmp_path, "v1.3.3", "v1.4.0")
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Phase 63 — v1.4.0\n\nNew stuff.\n", encoding="utf-8")
    _write_versioning(tmp_path, "# Versioning\n\n## v1.4.0 additive surfaces\n\nStuff.\n")
    ok, detail = rg.check_changelog_versioning_sync(tmp_path, "v1.4.0")
    assert ok, detail


# ---------------------------------------------------------------------------
# check_benchmarks_fresh_on_minor (todo.md item 22)
# ---------------------------------------------------------------------------

def test_benchmarks_fresh_on_minor_skipped_entirely_for_a_patch_release(tmp_path):
    _init_repo_with_two_tags(tmp_path, "v1.3.3", "v1.3.4")
    ok, detail = rg.check_benchmarks_fresh_on_minor(tmp_path, "v1.3.4")
    assert ok
    assert "PATCH-only" in detail


def test_benchmarks_fresh_on_minor_passes_with_an_unchanged_attestation(tmp_path):
    _init_repo_with_two_tags(tmp_path, "v1.3.3", "v1.4.0")
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Phase 63 — v1.4.0\n\nBenchmarks: unchanged\n", encoding="utf-8")
    ok, detail = rg.check_benchmarks_fresh_on_minor(tmp_path, "v1.4.0")
    assert ok, detail


def test_benchmarks_fresh_on_minor_fails_when_captured_sha_predates_the_previous_tag(tmp_path):
    first_sha = _init_repo_with_two_tags(tmp_path, "v1.3.3", "v1.4.0")
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Phase 63 — v1.4.0\n\nNew stuff, no benchmark note.\n", encoding="utf-8")
    (tmp_path / "development" / "BENCHMARKS.md").write_text(
        f"**Captured:** old, on Linux. Aether-Vault @ `{first_sha[:7]}`, git-lfs 3.7.1.\n",
        encoding="utf-8",
    )
    ok, detail = rg.check_benchmarks_fresh_on_minor(tmp_path, "v1.4.0")
    assert not ok
    assert "already predates the PREVIOUS tag" in detail


def test_benchmarks_fresh_on_minor_passes_when_recaptured_after_the_previous_tag(tmp_path):
    _init_repo_with_two_tags(tmp_path, "v1.3.3", "v1.4.0")
    second_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                                capture_output=True, text=True, check=True).stdout.strip()
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Phase 63 — v1.4.0\n\nNew stuff.\n", encoding="utf-8")
    (tmp_path / "development" / "BENCHMARKS.md").write_text(
        f"**Captured:** new, on Linux. Aether-Vault @ `{second_sha[:7]}`, git-lfs 3.7.1.\n",
        encoding="utf-8",
    )
    ok, detail = rg.check_benchmarks_fresh_on_minor(tmp_path, "v1.4.0")
    assert ok, detail


# ---------------------------------------------------------------------------
# _required_contexts fallback-file parsing (todo.md item 20, no network needed)
# ---------------------------------------------------------------------------

def test_required_contexts_reads_the_fallback_file_ignoring_comments_and_blanks(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, "_fetch_required_contexts_live", lambda repo, token: None)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "required-checks.txt").write_text(
        "# a comment\n\ntest (3.10)\ntest (3.14)\n", encoding="utf-8")
    contexts, source = rg._required_contexts(tmp_path, "owner/repo", None)
    assert contexts == ["test (3.10)", "test (3.14)"]
    assert "fallback file" in source


def test_required_contexts_prefers_the_live_api_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, "_fetch_required_contexts_live", lambda repo, token: ["live-check"])
    contexts, source = rg._required_contexts(tmp_path, "owner/repo", None)
    assert contexts == ["live-check"]
    assert "live" in source


# ---------------------------------------------------------------------------
# write_report (todo.md item 23)
# ---------------------------------------------------------------------------

def test_write_report_renders_a_markdown_table_and_overall_verdict(tmp_path):
    report_path = tmp_path / "release-gate-report.md"
    checks = [
        ("check one", (True, "all good")),
        ("check two", (False, "broke | with a pipe\nand a newline")),
    ]
    rg.write_report(report_path, "v1.3.4", checks)
    text = report_path.read_text(encoding="utf-8")
    assert "v1.3.4" in text
    assert "check one" in text and "✅ PASS" in text
    assert "check two" in text and "❌ FAIL" in text
    assert "**Overall: FAILED**" in text


# ---------------------------------------------------------------------------
# _evaluate_required_checks (pure classification, no network) and
# check_required_checks_green's poll/retry/fail-fast loop (network injected out via
# fetch_fn/sleep_fn) -- Probleme.md: a single point-in-time check used to fail a real
# release the moment any required check was merely still `in_progress`.
# ---------------------------------------------------------------------------

def _run(name, status, conclusion, started_at="2020-01-01T00:00:00Z"):
    return {"name": name, "status": status, "conclusion": conclusion, "started_at": started_at}


def test_evaluate_required_checks_green_when_all_succeed():
    runs = [_run("a", "completed", "success"), _run("b", "completed", "success")]
    state, detail = rg._evaluate_required_checks(["a", "b"], runs, "src", "v1")
    assert state == "green"
    assert "all 2 required" in detail


def test_evaluate_required_checks_pending_when_still_in_progress():
    state, detail = rg._evaluate_required_checks(["a"], [_run("a", "in_progress", None)], "src", "v1")
    assert state == "pending"
    assert "in_progress" in detail


def test_evaluate_required_checks_pending_when_check_run_hasnt_registered_yet():
    state, detail = rg._evaluate_required_checks(["a"], [], "src", "v1")
    assert state == "pending"
    assert "no check-run found" in detail


def test_evaluate_required_checks_failed_when_completed_unsuccessfully():
    state, detail = rg._evaluate_required_checks(["a"], [_run("a", "completed", "failure")], "src", "v1")
    assert state == "failed"
    assert "failure" in detail


def test_evaluate_required_checks_uses_most_recent_run_per_name():
    runs = [
        _run("a", "completed", "failure", started_at="2020-01-01T00:00:00Z"),
        _run("a", "completed", "success", started_at="2020-01-02T00:00:00Z"),
    ]
    state, _detail = rg._evaluate_required_checks(["a"], runs, "src", "v1")
    assert state == "green"


def test_check_required_checks_green_returns_immediately_when_already_green(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, "_required_contexts", lambda repo_root, repo, token: (["a"], "src"))
    calls = {"fetch": 0, "sleep": 0}

    def fetch():
        calls["fetch"] += 1
        return [_run("a", "completed", "success")], None

    ok, detail = rg.check_required_checks_green(
        "o/r", "v1", None, tmp_path, timeout_seconds=100, poll_interval=1,
        sleep_fn=lambda s: calls.__setitem__("sleep", calls["sleep"] + 1), fetch_fn=fetch,
    )
    assert ok, detail
    assert calls == {"fetch": 1, "sleep": 0}  # no polling needed at all


def test_check_required_checks_green_polls_through_pending_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, "_required_contexts", lambda repo_root, repo, token: (["a"], "src"))
    responses = [
        ([_run("a", "in_progress", None)], None),
        ([_run("a", "queued", None)], None),
        ([_run("a", "completed", "success")], None),
    ]
    sleeps = []
    ok, detail = rg.check_required_checks_green(
        "o/r", "v1", None, tmp_path, timeout_seconds=100, poll_interval=1,
        sleep_fn=sleeps.append, fetch_fn=lambda: responses.pop(0),
    )
    assert ok, detail
    assert sleeps == [1, 1]  # two rounds of pending before the third fetch went green


def test_check_required_checks_green_fails_fast_without_waiting_out_the_timeout(tmp_path, monkeypatch):
    """A definitive failure must return immediately -- retrying achieves nothing, and
    a huge timeout_seconds here proves the loop isn't just happening to finish fast."""
    monkeypatch.setattr(rg, "_required_contexts", lambda repo_root, repo, token: (["a"], "src"))
    sleeps = []
    ok, detail = rg.check_required_checks_green(
        "o/r", "v1", None, tmp_path, timeout_seconds=9999, poll_interval=1,
        sleep_fn=sleeps.append, fetch_fn=lambda: ([_run("a", "completed", "failure")], None),
    )
    assert not ok
    assert "failed" in detail
    assert sleeps == []


def test_check_required_checks_green_times_out_when_stuck_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, "_required_contexts", lambda repo_root, repo, token: (["a"], "src"))

    def tiny_real_sleep(_seconds):
        time.sleep(0.01)  # a real (tiny) sleep so the monotonic deadline actually advances

    ok, detail = rg.check_required_checks_green(
        "o/r", "v1", None, tmp_path, timeout_seconds=0.03, poll_interval=0.01,
        sleep_fn=tiny_real_sleep, fetch_fn=lambda: ([_run("a", "in_progress", None)], None),
    )
    assert not ok
    assert "timed out" in detail


def test_check_required_checks_green_propagates_a_fetch_error_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, "_required_contexts", lambda repo_root, repo, token: (["a"], "src"))
    ok, detail = rg.check_required_checks_green(
        "o/r", "v1", None, tmp_path, fetch_fn=lambda: (None, "network exploded"), sleep_fn=lambda s: None,
    )
    assert not ok
    assert "network exploded" in detail
