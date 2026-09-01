import statistics

import pytest

from python.av_cli import speedcheck
from python.av_cli.main import iter_working_files, load_config


def test_budget_for_matches_known_label_prefixes():
    assert speedcheck._budget_for("Index.save() (500 entries)") == 150.0
    assert speedcheck._budget_for("Index.load() (500 entries)") == 150.0
    assert speedcheck._budget_for("load_config()") == 50.0
    assert speedcheck._budget_for("iter_working_files() (2000 files)") == 200.0
    assert speedcheck._budget_for("Storage stats (1000 objs)") == 1000.0
    assert speedcheck._budget_for("semdiff.diff_trees() (500 entries)") == 100.0
    # v1.2.5: per-surface probes
    assert speedcheck._budget_for("commit_staged() (300 entries)") == 250.0
    assert speedcheck._budget_for("compute_status() (2000 files)") == 600.0
    assert speedcheck._budget_for("log() (150 commits)") == 300.0


def test_budget_for_returns_none_for_an_unknown_label():
    assert speedcheck._budget_for("some unrelated probe") is None


def test_budget_class_for_splits_cpu_from_disk():
    # semdiff is pure in-memory dict work -- the one "cpu" probe today.
    assert speedcheck._budget_class_for("semdiff.diff_trees() (500 entries)") == "cpu"
    # Everything else is filesystem-bound -- "disk", including new v1.2.5 probes.
    for label in ("Index.save() (500 entries)", "Index.load() (500 entries)",
                  "load_config()", "iter_working_files() (2000 files)",
                  "Storage stats (1000 objs)", "commit_staged() (300 entries)",
                  "compute_status() (2000 files)", "log() (150 commits)"):
        assert speedcheck._budget_class_for(label) == "disk", label


def test_budget_class_for_defaults_unknown_labels_to_disk():
    # Unrecognized labels default to the more lenient class rather than raising or getting
    # silently excluded from classification.
    assert speedcheck._budget_class_for("some unrelated probe") == "disk"


def test_run_synthetic_probes_returns_one_entry_per_probe_with_sane_shape(tmp_path):
    results = speedcheck.run_synthetic_probes(load_config, iter_working_files, tmp_path)

    assert len(results) == 9  # v1.2.5: + commit_staged()/compute_status()/log()
    labels = [label for label, _, _ in results]
    assert any("Index.save()" in label for label in labels)
    assert any("Index.load()" in label for label in labels)
    assert any("load_config()" in label for label in labels)
    assert any("iter_working_files()" in label for label in labels)
    assert any("Storage stats" in label for label in labels)
    assert any(label.startswith("semdiff.diff_trees()") for label in labels)
    assert any(label.startswith("commit_staged()") for label in labels)
    assert any(label.startswith("compute_status()") for label in labels)
    assert any(label.startswith("log()") for label in labels)

    for label, elapsed_ms, budget_ms in results:
        assert isinstance(elapsed_ms, float)
        assert elapsed_ms >= 0
        # Every synthetic probe label has a matching budget in _BUDGETS_MS — none should be
        # silently unbudgeted (that would mean the label and the budget table drifted apart).
        assert budget_ms is not None, f"no budget found for label: {label!r}"


def test_commit_and_log_probe_fixtures_are_real_not_stubs(tmp_path):
    # One run_synthetic_probes() call, two assertions -- both new v1.2.5 fixture-building
    # probes (commit_staged(), log()) must produce genuinely usable artifacts, not just a
    # number: a commit object must land on disk under the synthetic repo commit_staged()
    # builds (proving it never touched the network -- defer_upload=True + a deliberately
    # closed remote_url -- and really exercises the commit path), and log()'s synthetic
    # commits must form a genuine first-parent chain that walk_history() can traverse end
    # to end, not just N unlinked files.
    from python.av_cli import history

    speedcheck.run_synthetic_probes(load_config, iter_working_files, tmp_path)

    commits_dir = tmp_path / "commit_probe" / ".av" / "commits"
    assert commits_dir.is_dir()
    assert list(commits_dir.glob("*.json")), "commit_staged() probe produced no commit object"

    log_dir = tmp_path / "log_probe"
    tip = (log_dir / ".av" / "refs" / "heads" / "main").read_text().strip()
    walked = history.walk_history(log_dir, tip, speedcheck.SYNTHETIC_LOG_COMMIT_COUNT)
    assert len(walked) == speedcheck.SYNTHETIC_LOG_COMMIT_COUNT


def test_run_synthetic_probes_sampled_drops_the_warmup_and_returns_medians(tmp_path):
    results = speedcheck.run_synthetic_probes_sampled(
        load_config, iter_working_files, tmp_path, samples=3,
    )
    assert len(results) == 9
    for label, samples, median_ms, budget_ms, budget_class in results:
        assert isinstance(label, str)
        # samples=3 requested, 1 discarded as warm-up -> 2 kept.
        assert len(samples) == 2
        assert median_ms == statistics.median(samples)
        assert budget_ms is not None
        assert budget_class in ("cpu", "disk")


def test_run_synthetic_probes_sampled_keeps_all_samples_when_only_one_requested(tmp_path):
    # samples=1 has no warm-up to discard from -- the single run IS the result.
    results = speedcheck.run_synthetic_probes_sampled(
        load_config, iter_working_files, tmp_path, samples=1,
    )
    for _, samples, median_ms, _, _ in results:
        assert len(samples) == 1
        assert median_ms == samples[0]


def test_run_synthetic_probes_sampled_rejects_zero_samples(tmp_path):
    with pytest.raises(ValueError):
        speedcheck.run_synthetic_probes_sampled(load_config, iter_working_files, tmp_path, samples=0)


def test_run_synthetic_probes_is_repeatable_across_calls_in_fresh_dirs(tmp_path):
    # Fixture sizes are fixed constants (SYNTHETIC_ENTRY_COUNT etc.) — two independent runs
    # against two fresh tmp_roots should produce the same number/shape of probes every time.
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()

    results_a = speedcheck.run_synthetic_probes(load_config, iter_working_files, root_a)
    results_b = speedcheck.run_synthetic_probes(load_config, iter_working_files, root_b)

    labels_a = [label for label, _, _ in results_a]
    labels_b = [label for label, _, _ in results_b]
    assert labels_a == labels_b


def test_storage_stats_counts_objects_commits_and_refs(tmp_path):
    av_dir = tmp_path / ".av"
    objects_dir = av_dir / "objects" / "ab"
    objects_dir.mkdir(parents=True)
    (objects_dir / "cdef").write_bytes(b"x" * 100)

    commits_dir = av_dir / "commits"
    commits_dir.mkdir()
    (commits_dir / "deadbeef.json").write_text("{}")

    refs_dir = av_dir / "refs" / "heads"
    refs_dir.mkdir(parents=True)
    (refs_dir / "main").write_text("deadbeef")

    stats = speedcheck.storage_stats(av_dir)
    assert stats["total_objects"] == 1
    assert stats["total_size_bytes"] == 100
    assert stats["total_commits"] == 1
    assert stats["total_refs"] == 1


def test_storage_stats_handles_a_missing_av_dir_without_crashing(tmp_path):
    stats = speedcheck.storage_stats(tmp_path / "does-not-exist")
    assert stats == {
        "total_objects": 0,
        "total_commits": 0,
        "total_refs": 0,
        "total_size_bytes": 0,
    }


def test_run_real_repo_probes_against_an_initialized_repo(repo):
    probes = speedcheck.run_real_repo_probes(repo, load_config, iter_working_files)
    labels = [label for label, _ in probes]
    assert any("Index.load()" in label for label in labels)
    assert any(label == "load_config()" for label in labels)
    for _, elapsed_ms in probes:
        assert elapsed_ms >= 0
