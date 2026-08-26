"""Performance regression gate (v1.2.1; strictened to 2× in v1.2.2, relaxed to 2.5× in v1.2.4).

The budgets themselves live in python/av_cli/speedcheck.py (_budget_for) and are tuned
for typical dev machines. CI runners vary, so the gate multiplies each budget — 2.5× as
of v1.2.4 (was 2×): tight enough to catch substantial regressions early (the #70/#68
class: accidental re-scan, lost fast-paths, quadratic semdiff), loose enough for
shared-runner noise (Windows Py 3.14 hit 107ms on a 50ms budget — within 2.5× but over
2×). A failure here means a hot path got structurally slower and must be investigated
before release.
"""
import pytest

from python.av_cli import speedcheck
from python.av_cli.main import iter_working_files, load_config

BUDGET_MULTIPLIER = 2.5


def test_hot_paths_within_budget(tmp_path):
    results = speedcheck.run_synthetic_probes(load_config, iter_working_files, tmp_path)
    assert results, "speedcheck produced no probes"
    # The semdiff probe (v1.2.2) must be part of the gated family:
    assert any(label.startswith("semdiff.diff_trees()") for label, _, _ in results), \
        "semdiff probe missing from speedcheck synthetic probes"
    violations = []
    for label, elapsed_ms, budget_ms in results:
        budget = budget_ms if budget_ms is not None else speedcheck._budget_for(label)
        if budget is None:
            continue
        if elapsed_ms > budget * BUDGET_MULTIPLIER:
            violations.append(f"{label}: {elapsed_ms:.0f}ms > {BUDGET_MULTIPLIER:g}×{budget:.0f}ms")
    assert not violations, (
        "hot-path regression detected:\n  " + "\n  ".join(violations)
    )
