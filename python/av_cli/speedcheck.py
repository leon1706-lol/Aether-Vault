"""Timing probes for av's hot paths.

Used by `av doctor --speed` (real repo, read-only snapshot) and `av test --speed`
(synthetic fixtures, regression-trackable across runs). Pure logic only — no click,
no printing — so both commands can format the results however fits their own output
style. Takes `load_config`/`iter_working_files` as parameters rather than importing
them from `.main`, to avoid a circular import (main.py imports this module).
"""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable

from .index import Index

Probe = tuple[str, float]  # (label, elapsed_ms)

SYNTHETIC_ENTRY_COUNT = 500
SYNTHETIC_FILE_COUNT = 2000
SYNTHETIC_OBJECT_COUNT = 1000

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
}


def _budget_for(label: str) -> float | None:
    for prefix, budget in _BUDGETS_MS.items():
        if label.startswith(prefix):
            return budget
    return None


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

    return results


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
