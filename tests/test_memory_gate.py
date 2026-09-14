"""Peak-RSS regression gate (V1.6.3) -- the memory twin of test_perf_gate.py.

Opt-in (`AV_MEMORY_GATE=1`): peak RSS varies 10-20% between CPython allocators/OS versions
and the perf gate's own history (its module docstring) is what happens when a
machine-dependent number sits on the hard CI path. The `memory-budget` CI job runs it with
`continue-on-error`; locally: `AV_MEMORY_GATE=1 pytest tests/test_memory_gate.py -v`.

Same evidence rule as the perf gate: median of the kept samples over budget AND at least
_MIN_SAMPLES_OVER_BUDGET individual samples over, never a single freak run. Budgets live in
python/av_cli/speedcheck.py::_MEMORY_BUDGETS_MB (measured values x1.25, see
development/MEMORY.md); AV_MEMORY_BUDGET_MULTIPLIER (default 1.5) scales them all.
"""
import importlib.util
import os
import shutil
import statistics
import sys
from pathlib import Path

import pytest

from python.av_cli import speedcheck, sysres

SAMPLES = 3  # first is discarded as warm-up
_MIN_SAMPLES_OVER_BUDGET = 2
DEFAULT_MULTIPLIER = 1.5

pytestmark = pytest.mark.skipif(os.environ.get("AV_MEMORY_GATE") != "1",
                                reason="opt-in: set AV_MEMORY_GATE=1")

_SCOREBOARD = Path(__file__).resolve().parents[1] / "scripts" / "rss_scoreboard.py"


def _scoreboard_module():
    spec = importlib.util.spec_from_file_location("rss_scoreboard", _SCOREBOARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _multiplier() -> float:
    override = os.environ.get("AV_MEMORY_BUDGET_MULTIPLIER")
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    return DEFAULT_MULTIPLIER


def _av() -> str:
    av = shutil.which("av")
    if av is None:
        pytest.skip("av not on PATH")
    return av


def _env() -> dict:
    env = dict(os.environ)
    env["AV_NO_DAEMON"] = "1"
    env["NO_COLOR"] = "1"
    return env


def _measure(args: list[str], cwd: Path, prepare=None) -> list[float]:
    peaks = []
    for i in range(SAMPLES):
        if prepare is not None:
            prepare(i)
        run = sysres.run_measured(args, cwd=str(cwd), env=_env(), capture_output=True, timeout=1800)
        assert run.returncode == 0, f"{args} failed: {run.stdout}\n{run.stderr}"
        if run.source == "unavailable" or run.peak_rss_mb is None:
            pytest.skip("no child RSS probe on this platform")
        peaks.append(run.peak_rss_mb)
    return peaks[1:]  # warm-up discarded


def test_memory_budgets_cover_every_scenario():
    assert set(speedcheck._MEMORY_BUDGETS_MB) == {"status_cold", "add_safetensors_64mib",
                                                  "add_many_safetensors", "commit"}


def test_memory_scenarios_within_budget(tmp_path):
    av = _av()
    sb = _scoreboard_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    init = sysres.run_measured([av, "init", "--mode", "local", "--yes", "--no-repl"], cwd=str(repo),
                               env=_env(), capture_output=True, timeout=300)
    assert init.returncode == 0, init.stderr
    sb.populate_small_files(repo, 300)
    assert sysres.run_measured([av, "add", "."], cwd=str(repo), env=_env(), capture_output=True,
                               timeout=600).returncode == 0

    results: dict[str, list[float]] = {}
    results["status_cold"] = _measure([av, "status"], repo)

    model = tmp_path / "m64.safetensors"
    sb.write_synthetic_safetensors(model, n_layers=2, layer_bytes=32 * 1024 * 1024)

    def fresh_single(i: int) -> None:
        dest = repo / f"single_{i}.safetensors"
        shutil.copyfile(model, dest)
        with open(dest, "r+b") as f:
            f.seek(4096 + i)
            f.write(b"\xff")

    results["add_safetensors_64mib"] = _measure([av, "add", "."], repo, prepare=fresh_single)

    def fresh_many(i: int) -> None:
        d = repo / f"many_{i}"
        d.mkdir()
        for j in range(4):
            sb.write_synthetic_safetensors(d / f"s{j}.safetensors", n_layers=1, layer_bytes=32 * 1024 * 1024,
                                           seed=100 * i + j)

    results["add_many_safetensors"] = _measure([av, "add", "."], repo, prepare=fresh_many)

    def touch(i: int) -> None:
        (repo / "src" / f"gate_{i}.py").write_text(f"v = {i}\n", encoding="utf-8")
        assert sysres.run_measured([av, "add", "."], cwd=str(repo), env=_env(), capture_output=True,
                                   timeout=600).returncode == 0

    results["commit"] = _measure([av, "commit", "-m", "gate", "--no-upload"], repo, prepare=touch)

    multiplier = _multiplier()
    violations = []
    for label, samples in results.items():
        budget = speedcheck._memory_budget_for(label)
        assert budget is not None, f"no memory budget for {label}"
        threshold = budget * multiplier
        median = statistics.median(samples)
        over = sum(1 for s in samples if s > threshold)
        if median > threshold and over >= min(_MIN_SAMPLES_OVER_BUDGET, len(samples)):
            violations.append(f"{label}: median {median:.0f} MB > {multiplier:g}x{budget:.0f} MB "
                              f"({over}/{len(samples)} over) -- samples {samples}")
    assert not violations, "peak-RSS regression detected:\n  " + "\n  ".join(violations)
