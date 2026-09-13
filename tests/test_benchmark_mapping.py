"""Guards the exact bug found in the V1.6.0 benchmark-methodology audit: V1.5.0 inserted a
new `av --version` probe at index 0 of `speedcheck.run_av_cli_probes()`, but
`bench_commit_push_latency.py` and `scripts/run_benchmark_comparison.py` kept reading
`probes[0]`/`probes[1]`/`probes[2]` positionally -- silently shifting every label one slot
(the published "commit" row was actually `av add .`'s time, and the real `av commit`
number was discarded). `speedcheck.probe_ms()` looks a probe up by its label instead, and
this file is what would have caught the original bug: it fails if any consumer maps an
`av init`/`av add .`/`av commit` label to the wrong argv's timing.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from av_cli import speedcheck  # noqa: E402

import benchmarks.bench_commit_push_latency as bench_commit_push_latency  # noqa: E402


def test_probe_ms_finds_a_probe_by_label_prefix():
    probes = [("av --version", 1.0), ("av init", 2.0), ("av add . (60 files)", 3.0), ("av commit", 4.0)]
    assert speedcheck.probe_ms(probes, "av init") == 2.0
    assert speedcheck.probe_ms(probes, "av add .") == 3.0
    assert speedcheck.probe_ms(probes, "av commit") == 4.0
    assert speedcheck.probe_ms(probes, "av --version") == 1.0


def test_probe_ms_returns_none_for_an_unmatched_label():
    assert speedcheck.probe_ms([("av init", 2.0)], "av commit") is None


def test_probe_ms_is_robust_to_a_probe_being_inserted_at_the_front():
    """The exact regression: a new probe pushed onto the front of the list must not change
    what any existing label resolves to."""
    before = [("av init", 2.0), ("av add . (60 files)", 3.0), ("av commit", 4.0)]
    after = [("av --version", 1.0), *before]  # simulates the V1.5.0 insertion
    for label in ("av init", "av add .", "av commit"):
        assert speedcheck.probe_ms(before, label) == speedcheck.probe_ms(after, label)


def test_run_av_cli_probes_maps_init_add_commit_to_the_matching_argv(monkeypatch, tmp_path):
    """End-to-end guard on the real function: fakes `subprocess.run` to record which argv
    each call actually ran, and asserts speedcheck.probe_ms recovers the right timing for
    each of init/add/commit by label -- not by position."""
    recorded_argvs: list[list[str]] = []

    # Distinct, recognizable "durations" per argv so a mislabeled mapping is loud, not subtle.
    def fake_run(args, cwd=None, env=None):
        recorded_argvs.append(list(args))

        class _Result:
            returncode = 0
        return _Result()

    times = iter([
        0.0, 0.001,   # --version:  1ms
        1.0, 1.100,   # init:     100ms
        2.0, 2.500,   # add:      500ms
        3.0, 3.900,   # commit:   900ms
    ])
    monkeypatch.setattr(speedcheck.subprocess, "run", fake_run)
    monkeypatch.setattr(speedcheck.time, "perf_counter", lambda: next(times))
    monkeypatch.setattr(speedcheck, "populate_cli_fixture", lambda root: None)

    probes = speedcheck.run_av_cli_probes("av", tmp_path)

    assert speedcheck.probe_ms(probes, "av --version") == pytest.approx(1.0, abs=1.0)
    assert speedcheck.probe_ms(probes, "av init") == pytest.approx(100.0, abs=1.0)
    assert speedcheck.probe_ms(probes, "av add .") == pytest.approx(500.0, abs=1.0)
    assert speedcheck.probe_ms(probes, "av commit") == pytest.approx(900.0, abs=1.0)

    # And the argv actually run for "commit" is a commit, never an add or an init.
    commit_argv = recorded_argvs[3]
    assert commit_argv[1] == "commit"


def test_bench_commit_push_latency_uses_probe_ms_not_positional_indexing(monkeypatch, tmp_path):
    """Regression test for the actual shipped bug: run `_bench_av()` against a fake
    `run_av_cli_probes` that returns labels in a different order than a naive
    `probes[0]/probes[1]/probes[2]` read would assume, and assert the result still maps
    each operation to its own real timing."""
    fake_probes = [
        ("av --version", 111.0),
        ("av init", 222.0),
        ("av add . (60 files)", 333.0),
        ("av commit", 444.0),
    ]
    monkeypatch.setattr(speedcheck, "run_av_cli_probes", lambda *a, **k: fake_probes)
    monkeypatch.setattr(bench_commit_push_latency, "time_subprocess", lambda *a, **k: 555.0)
    monkeypatch.setattr(bench_commit_push_latency.shutil, "which", lambda name: "av" if name == "av" else None)
    monkeypatch.setattr(bench_commit_push_latency.subprocess, "run", lambda *a, **k: None)

    result = bench_commit_push_latency._bench_av()

    assert result == {"init": 222.0, "add": 333.0, "commit": 444.0, "push": 555.0}
