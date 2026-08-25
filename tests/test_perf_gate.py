"""Performance regression gate (v1.2.1): hot-path probes must land within 3× budget.

The budgets themselves live in python/av_cli/speedcheck.py (_budget_for) and are tuned
for typical dev machines. CI runners vary, so this gate uses a generous 3× multiplier —
tight enough to catch order-of-magnitude regressions (the #70/#68 class: accidental
re-scan, lost fast-paths), loose enough to stay stable under shared-runner noise.
A failure here means a hot path got structurally slower and must be investigated
before release.
"""
import pytest

from python.av_cli import speedcheck
from python.av_cli.main import iter_working_files, load_config

BUDGET_MULTIPLIER = 3.0


def test_hot_paths_within_3x_budget(tmp_path):
    results = speedcheck.run_synthetic_probes(load_config, iter_working_files, tmp_path)
    assert results, "speedcheck produced no probes"
    violations = []
    for label, elapsed_ms, budget_ms in results:
        budget = budget_ms if budget_ms is not None else speedcheck._budget_for(label)
        if budget is None:
            continue
        if elapsed_ms > budget * BUDGET_MULTIPLIER:
            violations.append(f"{label}: {elapsed_ms:.0f}ms > 3×{budget:.0f}ms")
    assert not violations, (
        "hot-path regression detected:\n  " + "\n  ".join(violations)
    )
