"""Performance regression gate (v1.2.1; strictened to 2x in v1.2.2, relaxed to 2.5x in
v1.2.4; replaced with a median-of-N, per-class scheme in v1.2.5).

The budgets themselves live in python/av_cli/speedcheck.py (_budget_for) and are tuned
for typical dev machines. History of the single global multiplier this gate used to apply
on top of them: 3.0 (v1.2.1) -> 2.0 (v1.2.2, too tight -- a Windows Py3.14 runner hit 107ms
on a 50ms load_config() budget) -> 2.5 (v1.2.4, commit 4df5c06). Loosening the same number
a fourth time would have kept trading away the gate's ability to catch a real regression
(the #70/#68 class: accidental re-scan, lost fast-paths, quadratic semdiff) just to survive
one noisy machine. v1.2.5 replaces that with three changes instead:

  1. Median-of-N (speedcheck.run_synthetic_probes_sampled): each probe runs PROBE_SAMPLES
     times, the first is discarded as a warm-up, and the median of the rest is what gets
     judged -- a single slow tick (disk cache miss, one antivirus scan, a GC pause) can no
     longer trip the gate on its own.
  2. A genuine cpu/disk split (CPU_MULTIPLIER/DISK_MULTIPLIER below): semdiff's pure
     in-memory dict work is far more repeatable across runs/machines than filesystem I/O,
     so it gets a tighter multiplier instead of being sized to the worst-case disk probe.
  3. A per-OS adjustment: Windows disk probes get a further bump
     (WINDOWS_DISK_EXTRA_MULTIPLIER) -- the documented noise source (antivirus + small-file
     I/O latency) is Windows-and-disk specific, not a property of every probe everywhere.

A failure here means a hot path got structurally slower (median exceeded budget AND at
least 2 of the N samples did too -- see _has_enough_evidence below) and must be
investigated before release. AV_PERF_BUDGET_MULTIPLIER (documented in
development/infrastructure.md) overrides both class multipliers outright for a genuinely
slow/noisy machine, instead of stacking with them -- one number is the whole story for
that run.
"""
import os
import platform

import pytest

from python.av_cli import speedcheck
from python.av_cli.main import iter_working_files, load_config

CPU_MULTIPLIER = 2.0
DISK_MULTIPLIER = 3.0
WINDOWS_DISK_EXTRA_MULTIPLIER = 1.5

# A regression must show up in at least this many of the N kept samples, not just the
# median crossing the line by itself -- keeps one freak sample among an otherwise-fine
# vector from being read as "the median moved" when it didn't really.
_MIN_SAMPLES_OVER_BUDGET = 2


def _multiplier_for(budget_class: str) -> float:
    override = os.environ.get("AV_PERF_BUDGET_MULTIPLIER")
    if override:
        try:
            return float(override)
        except ValueError:
            pass  # malformed override -- fall through to the real defaults rather than crash the gate
    multiplier = CPU_MULTIPLIER if budget_class == "cpu" else DISK_MULTIPLIER
    if budget_class == "disk" and platform.system() == "Windows":
        multiplier *= WINDOWS_DISK_EXTRA_MULTIPLIER
    return multiplier


def test_hot_paths_within_budget(tmp_path):
    results = speedcheck.run_synthetic_probes_sampled(load_config, iter_working_files, tmp_path)
    assert results, "speedcheck produced no probes"
    labels = [label for label, *_ in results]
    # These assertions exist so a probe silently dropping out of the battery fails
    # loudly here instead of quietly shrinking gate coverage.
    assert any(label.startswith("semdiff.diff_trees()") for label in labels), \
        "semdiff probe missing from speedcheck synthetic probes"
    assert any(label.startswith("commit_staged()") for label in labels), \
        "commit_staged probe missing from speedcheck synthetic probes"
    assert any(label.startswith("compute_status()") for label in labels), \
        "compute_status probe missing from speedcheck synthetic probes"
    assert any(label.startswith("log()") for label in labels), \
        "log probe missing from speedcheck synthetic probes"

    violations = []
    for label, samples, median_ms, budget_ms, budget_class in results:
        budget = budget_ms if budget_ms is not None else speedcheck._budget_for(label)
        if budget is None:
            continue
        multiplier = _multiplier_for(budget_class)
        threshold = budget * multiplier
        over_count = sum(1 for s in samples if s > threshold)
        if median_ms > threshold and over_count >= _MIN_SAMPLES_OVER_BUDGET:
            vector = ", ".join(f"{s:.0f}" for s in samples)
            violations.append(
                f"{label}: median {median_ms:.0f}ms > {multiplier:g}x{budget:.0f}ms "
                f"({budget_class}, {over_count}/{len(samples)} samples over) -- "
                f"run vector: [{vector}]ms"
            )
    assert not violations, (
        "hot-path regression detected:\n  " + "\n  ".join(violations)
    )
