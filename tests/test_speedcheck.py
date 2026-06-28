from python.av_cli import speedcheck
from python.av_cli.main import iter_working_files, load_config


def test_budget_for_matches_known_label_prefixes():
    assert speedcheck._budget_for("Index.save() (500 entries)") == 150.0
    assert speedcheck._budget_for("Index.load() (500 entries)") == 150.0
    assert speedcheck._budget_for("load_config()") == 50.0
    assert speedcheck._budget_for("iter_working_files() (2000 files)") == 200.0
    assert speedcheck._budget_for("Storage stats (1000 objs)") == 1000.0


def test_budget_for_returns_none_for_an_unknown_label():
    assert speedcheck._budget_for("some unrelated probe") is None


def test_run_synthetic_probes_returns_one_entry_per_probe_with_sane_shape(tmp_path):
    results = speedcheck.run_synthetic_probes(load_config, iter_working_files, tmp_path)

    assert len(results) == 5
    labels = [label for label, _, _ in results]
    assert any("Index.save()" in label for label in labels)
    assert any("Index.load()" in label for label in labels)
    assert any("load_config()" in label for label in labels)
    assert any("iter_working_files()" in label for label in labels)
    assert any("Storage stats" in label for label in labels)

    for label, elapsed_ms, budget_ms in results:
        assert isinstance(elapsed_ms, float)
        assert elapsed_ms >= 0
        # Every synthetic probe label has a matching budget in _BUDGETS_MS — none should be
        # silently unbudgeted (that would mean the label and the budget table drifted apart).
        assert budget_ms is not None, f"no budget found for label: {label!r}"


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
