"""Timing probes for av's hot paths.

Used by `av doctor --speed` (real repo, read-only snapshot) and `av test --speed`
(synthetic fixtures, regression-trackable across runs). Pure logic only — no click,
no printing — so both commands can format the results however fits their own output
style. Takes `load_config`/`iter_working_files` as parameters rather than importing
them from `.main`, to avoid a circular import (main.py imports this module).
"""

import json
import os
import statistics
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable

from .index import Index

Probe = tuple[str, float]  # (label, elapsed_ms)
# v1.2.5: (label, samples_ms, median_ms, budget_ms, budget_class) — the perf gate's
# median-of-N view of a probe. budget_class is "cpu" or "disk" (see _BUDGET_CLASS below);
# the gate (tests/test_perf_gate.py) owns what multiplier each class gets — this module
# only classifies, it never judges pass/fail.
SampledProbe = tuple[str, list, float, float | None, str]

SYNTHETIC_ENTRY_COUNT = 500
SYNTHETIC_FILE_COUNT = 2000
SYNTHETIC_OBJECT_COUNT = 1000
SYNTHETIC_COMMIT_ENTRY_COUNT = 300
# 150, not 500 — still 5x `av log`'s default --limit (30), enough depth to catch a real
# algorithmic regression (e.g. walk_history() turning quadratic), while keeping the
# probe's absolute cost reasonable: it opens+reads+json.loads() one file per commit
# sequentially (unlike iter_working_files()/Storage stats(), which only stat/enumerate),
# so its per-item cost is structurally higher and doesn't need as large an N to be useful.
SYNTHETIC_LOG_COMMIT_COUNT = 150

# v1.2.5: how many times the perf gate samples each probe (see run_synthetic_probes_sampled).
# The first sample is always discarded as a warm-up — first-touch disk cache / OS scheduler
# noise was the dominant source of the single-shot flakiness that forced the old single
# global BUDGET_MULTIPLIER up three times (3.0 -> 2.0 -> 2.5, see test_perf_gate.py's
# docstring) — median-of-N removes that without loosening the real threshold.
PROBE_SAMPLES = 5

# Soft advisory budgets in ms, keyed by label prefix — exceeding one only flags a
# row as SLOW in the printed table, it never fails the command or the test suite.
# Set generously above typical cold-disk timings (these are filesystem-bound, so vary a
# lot by machine/antivirus/disk) so a normal run is quiet and only a real regression —
# e.g. a multiple-times slowdown — trips the flag. Adjust to taste for your own hardware.
_BUDGETS_MS = {
    "Index.save()": 150.0,
    "Index.load()": 150.0,
    "load_config()": 50.0,
    "iter_working_files()": 200.0,
    "Storage stats": 1000.0,
    # v1.2.2: semdiff joined the hot-path family (it runs per handoff/diff and now also
    # inside the perf gate) — budget sized for the synthetic 500-entry tree below.
    "semdiff.diff_trees()": 100.0,
    # v1.2.5: per-surface probes the todo asked for — commit/status/log each get their own
    # budget instead of being implied by Index.save()/iter_working_files(). compute_status()
    # and log() were both revised upward from their first-cut estimates after a real
    # measurement run showed the initial guesses (300/150) were too tight even before
    # applying the disk-class multiplier — see test_perf_gate.py's docstring history for
    # why "measure, then set the budget" beats guessing here same as it does for the
    # multiplier itself.
    "commit_staged()": 250.0,
    "compute_status()": 600.0,
    "log()": 300.0,
}

# v1.2.5: which multiplier family (see test_perf_gate.py) each probe belongs to. CPU probes
# (pure in-memory work) are far more repeatable across runs/machines than disk probes
# (filesystem I/O — subject to antivirus scanning, cache state, and Windows' notoriously
# slow small-file I/O), so they get a tighter multiplier. Unrecognized labels default to
# "disk" — the more lenient class — rather than silently getting no classification.
_BUDGET_CLASS = {
    "semdiff.diff_trees()": "cpu",
}


def _budget_for(label: str) -> float | None:
    for prefix, budget in _BUDGETS_MS.items():
        if label.startswith(prefix):
            return budget
    return None


def _budget_class_for(label: str) -> str:
    for prefix, cls in _BUDGET_CLASS.items():
        if label.startswith(prefix):
            return cls
    return "disk"


def _time_ms(fn: Callable[[], object]) -> float:
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000


def storage_stats(av_dir: Path) -> dict:
    """Object/commit/ref counts under an `.av/` directory.

    Deliberately reimplemented here rather than imported from av_server.storage —
    av_cli has no runtime dependency on the server package, and this only needs
    the same counts, not the server's full CASStorage API.
    """
    objects_dir = av_dir / "objects"
    commits_dir = av_dir / "commits"
    refs_dir = av_dir / "refs"

    total_objects = 0
    total_size = 0
    if objects_dir.is_dir():
        for root, _, files in os.walk(objects_dir):
            for f in files:
                total_objects += 1
                total_size += (Path(root) / f).stat().st_size

    total_commits = len(list(commits_dir.glob("*.json"))) if commits_dir.is_dir() else 0
    total_refs = sum(len(files) for _, _, files in os.walk(refs_dir)) if refs_dir.is_dir() else 0

    return {
        "total_objects": total_objects,
        "total_commits": total_commits,
        "total_refs": total_refs,
        "total_size_bytes": total_size,
    }


def run_real_repo_probes(
    repo_root: Path,
    load_config: Callable[[Path], dict],
    iter_working_files: Callable[[Path], object],
) -> list[Probe]:
    """Read-only timing snapshot of the real, current repo. Never mutates `.av/`."""
    av_dir = repo_root / ".av"
    entry_count = len(Index(repo_root).entries)

    return [
        (f"Index.load() (.av/index, {entry_count} entries)", _time_ms(lambda: Index(repo_root))),
        ("load_config()", _time_ms(lambda: load_config(repo_root))),
        ("iter_working_files() (real tree)", _time_ms(lambda: list(iter_working_files(repo_root)))),
        ("Storage stats (.av/objects)", _time_ms(lambda: storage_stats(av_dir))),
    ]


def run_synthetic_probes(
    load_config: Callable[[Path], dict],
    iter_working_files: Callable[[Path], object],
    tmp_root: Path,
) -> list[tuple[str, float, float | None]]:
    """Timing probes against disposable synthetic fixtures under `tmp_root`.

    Repeatable and comparable across runs/machines, since fixture sizes are fixed
    constants rather than whatever happens to be in a real repo.
    """
    av_dir = tmp_root / ".av"
    av_dir.mkdir()
    results: list[tuple[str, float, float | None]] = []

    idx = Index(tmp_root)
    for i in range(SYNTHETIC_ENTRY_COUNT):
        idx.entries[f"file_{i}.py"] = {
            "hash": uuid.uuid4().hex,
            "size": 1024,
            "mtime_ns": 0,
            "type": "code",
            "staged": True,
            "pointer": None,
        }
    label = f"Index.save() ({SYNTHETIC_ENTRY_COUNT} entries)"
    results.append((label, _time_ms(idx.save), _budget_for(label)))

    label = f"Index.load() ({SYNTHETIC_ENTRY_COUNT} entries)"
    results.append((label, _time_ms(lambda: Index(tmp_root)), _budget_for(label)))

    config_path = av_dir / "config"
    config_path.write_text(json.dumps({"lfs_threshold_mb": 50, "remote_url": "http://localhost:8000"}))
    label = "load_config()"
    results.append((label, _time_ms(lambda: load_config(tmp_root)), _budget_for(label)))

    work_dir = tmp_root / "src"
    work_dir.mkdir()
    for i in range(SYNTHETIC_FILE_COUNT):
        (work_dir / f"f_{i}.txt").write_text("x")
    label = f"iter_working_files() ({SYNTHETIC_FILE_COUNT} files)"
    results.append((label, _time_ms(lambda: list(iter_working_files(tmp_root))), _budget_for(label)))

    objects_dir = av_dir / "objects"
    for i in range(SYNTHETIC_OBJECT_COUNT):
        h = uuid.uuid4().hex + uuid.uuid4().hex[:32]
        shard = objects_dir / h[:2]
        shard.mkdir(parents=True, exist_ok=True)
        (shard / h[2:]).write_bytes(b"x" * 64)
    label = f"Storage stats ({SYNTHETIC_OBJECT_COUNT} objs)"
    results.append((label, _time_ms(lambda: storage_stats(av_dir)), _budget_for(label)))

    # v1.2.2 probe: semantic-diff cost over a synthetic 500-entry tree pair (a third of
    # the entries carry chunk manifests, mirroring a checkpoint-heavy repo). Pure CPU —
    # this is the path `av diff`, .avh generation, and the webui summary all ride.
    from .semdiff import diff_trees

    def _synth_tree(chunked_every: int) -> dict:
        tree: dict = {}
        for i in range(SYNTHETIC_ENTRY_COUNT):
            entry = {
                "hash": uuid.uuid4().hex,
                "size": 1024,
                "type": "artifact" if i % 3 == 0 else "code",
                "layers": [],
                "chunks": [],
            }
            if i % 3 == 0:
                entry["chunks"] = [
                    {"hash": uuid.uuid4().hex, "size": 512, "offset": 0}
                    for _ in range(4)
                ]
            tree[f"file_{i}.bin"] = entry
        del chunked_every
        return tree

    old_tree = _synth_tree(3)
    new_tree = _synth_tree(3)
    # Keep half the population identical so the reuse branch is exercised, not just churn.
    for i in range(0, SYNTHETIC_ENTRY_COUNT, 2):
        new_tree[f"file_{i}.bin"] = dict(old_tree[f"file_{i}.bin"])
    label = f"semdiff.diff_trees() ({SYNTHETIC_ENTRY_COUNT} entries)"
    results.append((label, _time_ms(lambda: diff_trees(old_tree, new_tree)),
                    _budget_for(label)))

    # v1.2.5 per-surface probe: commit_staged() end-to-end — deterministic hash over sorted
    # JSON, atomic local persist, ref advance, index clear+resave. Imported lazily (not at
    # module level) because core.py imports this module (`from . import speedcheck`), so a
    # top-level `from .core import commit_staged` here would be circular; by the time this
    # function actually runs, core.py has always finished importing.
    from .core import commit_staged

    commit_dir = tmp_root / "commit_probe"
    commit_av_dir = commit_dir / ".av"
    (commit_av_dir / "commits").mkdir(parents=True)
    (commit_av_dir / "refs" / "heads").mkdir(parents=True)
    (commit_av_dir / "objects").mkdir()
    (commit_av_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (commit_av_dir / "refs" / "heads" / "main").write_text("")
    (commit_av_dir / "config").write_text(json.dumps({
        "lfs_threshold_mb": 50,
        # Deliberately a closed port, not the default localhost:8000 — this machine may
        # genuinely have a real av_server listening there (e.g. `docker compose up`), and
        # this probe must never depend on luck (or worse, push a synthetic commit into a
        # live registry) to stay network-free. defer_upload=True below skips the network
        # entirely regardless, but this is the belt to that braces.
        "remote_url": "http://127.0.0.1:1",
    }))
    commit_idx = Index(commit_dir)
    for i in range(SYNTHETIC_COMMIT_ENTRY_COUNT):
        commit_idx.entries[f"commit_file_{i}.py"] = {
            "hash": uuid.uuid4().hex,
            "size": 1024,
            "mtime_ns": 0,
            "type": "code",
            "staged": True,
            "pointer": None,
        }
    commit_idx.save()
    label = f"commit_staged() ({SYNTHETIC_COMMIT_ENTRY_COUNT} entries)"
    results.append((
        label,
        _time_ms(lambda: commit_staged(
            commit_dir, "speedcheck probe", defer_upload=True, result_sink=lambda _r: None,
        )),
        _budget_for(label),
    ))

    # v1.2.5 per-surface probe: compute_status() — reuses the 2000-file working tree already
    # built for iter_working_files() above (compute_status() calls iter_working_files()
    # internally plus a per-file stat/compare, so this measures that combined real cost) with
    # a fresh Index deliberately mismatched against it: every 4th file untracked (no entry),
    # the rest split staged/needs-compare, so all four status branches actually execute.
    from .core import compute_status

    status_idx = Index(tmp_root)
    for i in range(SYNTHETIC_FILE_COUNT):
        if i % 4 == 0:
            continue  # left untracked on purpose
        status_idx.entries[f"src/f_{i}.txt"] = {
            "hash": "deadbeef",
            "size": 1,
            "mtime_ns": 0,
            "type": "code",
            "staged": (i % 4 == 1),
            "pointer": None,
        }
    label = f"compute_status() ({SYNTHETIC_FILE_COUNT} files)"
    results.append((
        label, _time_ms(lambda: compute_status(tmp_root, status_idx)), _budget_for(label),
    ))

    # v1.2.5 per-surface probe: log() — walk_history() over a synthetic linear chain, built
    # by writing commit JSON directly (real commit_staged() calls would be far slower to set
    # up at this scale and the walk itself, not commit creation, is what this times).
    from . import history

    log_dir = tmp_root / "log_probe"
    log_commits_dir = log_dir / ".av" / "commits"
    log_commits_dir.mkdir(parents=True)
    log_refs_dir = log_dir / ".av" / "refs" / "heads"
    log_refs_dir.mkdir(parents=True)
    prev_hash: str | None = None
    tip_hash = ""
    for i in range(SYNTHETIC_LOG_COMMIT_COUNT):
        h = uuid.uuid4().hex + uuid.uuid4().hex[:32]  # 64 hex chars, sha256-shaped
        commit_data = {
            "hash": h,
            "parents": [prev_hash] if prev_hash else [],
            "author": "speedcheck",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "message": f"synthetic commit {i}",
            "tree": {},
            "tags": [],
            "metrics": {},
        }
        (log_commits_dir / f"{h}.json").write_text(json.dumps(commit_data))
        prev_hash = h
        tip_hash = h
    (log_dir / ".av" / "HEAD").write_text("ref: refs/heads/main\n")
    (log_refs_dir / "main").write_text(tip_hash)
    label = f"log() ({SYNTHETIC_LOG_COMMIT_COUNT} commits)"
    results.append((
        label,
        _time_ms(lambda: history.walk_history(log_dir, tip_hash, SYNTHETIC_LOG_COMMIT_COUNT)),
        _budget_for(label),
    ))

    return results


def run_synthetic_probes_sampled(
    load_config: Callable[[Path], dict],
    iter_working_files: Callable[[Path], object],
    tmp_root: Path,
    samples: int = PROBE_SAMPLES,
) -> list[SampledProbe]:
    """Median-of-N view of run_synthetic_probes(), for the perf gate (test_perf_gate.py).

    Runs the full probe battery `samples` times, each in its own fresh subdirectory (the
    probes create/mutate files, so a run can't reuse another run's directory), discards the
    first run as a warm-up (see PROBE_SAMPLES' docstring), and returns the median of the
    rest alongside the full sample vector and each probe's budget class — the gate applies
    its own tolerance policy (median-exceeds-budget AND >=2 samples over) on top of this
    rather than this module baking one policy in, so `av test --speed`'s single-shot
    printed table (run_synthetic_probes(), unchanged) and the gate's stricter view can
    each want different things without duplicating the probe bodies themselves.
    """
    if samples < 1:
        raise ValueError("samples must be >= 1")

    per_run: list[list[tuple[str, float, float | None]]] = []
    for i in range(samples):
        sample_dir = tmp_root / f"sample_{i}"
        sample_dir.mkdir()
        per_run.append(run_synthetic_probes(load_config, iter_working_files, sample_dir))

    # Drop the warm-up run when there's more than one sample to drop it from.
    kept = per_run[1:] if len(per_run) > 1 else per_run
    labels = [label for label, _, _ in per_run[0]]

    sampled: list[SampledProbe] = []
    for pos, label in enumerate(labels):
        values = [run[pos][1] for run in kept]
        budget = per_run[0][pos][2]
        sampled.append((label, values, statistics.median(values), budget, _budget_class_for(label)))
    return sampled


# ---------------------------------------------------------------------------
# av CLI, end-to-end (real subprocess timings, shared with
# scripts/run_benchmark_comparison.py's cross-tool README comparison)
# ---------------------------------------------------------------------------

CLI_CODE_FILE_COUNT = 50
CLI_CODE_FILE_SIZE = 1024  # 1 KB each — stand-in for source/config files
CLI_LARGE_FILE_COUNT = 10
CLI_LARGE_FILE_SIZE = 2 * 1024 * 1024  # 2 MB each — stand-in for model/dataset files


def populate_cli_fixture(root: Path) -> None:
    for i in range(CLI_CODE_FILE_COUNT):
        (root / f"src_{i}.py").write_text("x" * CLI_CODE_FILE_SIZE)
    for i in range(CLI_LARGE_FILE_COUNT):
        (root / f"model_{i}.bin").write_bytes(b"x" * CLI_LARGE_FILE_SIZE)


def run_av_cli_probes(av_path: str, tmp_root: Path) -> list[Probe]:
    """Times the real `av` binary (init/add/commit) as subprocesses against the shared
    CLI fixture. Caller resolves/checks `av_path` (e.g. via `shutil.which`) and provides
    a disposable `tmp_root` — this never touches a real repo.
    """
    populate_cli_fixture(tmp_root)
    file_count = CLI_CODE_FILE_COUNT + CLI_LARGE_FILE_COUNT
    steps = [
        ("av init", [av_path, "init", "--mode", "local", "--yes", "--no-repl"]),
        (f"av add . ({file_count} files)", [av_path, "add", "."]),
        ("av commit", [av_path, "commit", "-m", "speedcheck"]),
    ]
    results: list[Probe] = []
    for label, args in steps:
        start = time.perf_counter()
        subprocess.run(args, cwd=tmp_root)
        results.append((label, (time.perf_counter() - start) * 1000))
    return results
