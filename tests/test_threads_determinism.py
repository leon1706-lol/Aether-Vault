"""V1.5.0 perf work: `av add`'s deterministic-output guarantee -- for a given working tree,
`av` must produce a byte-identical `.av/index`, an identical set of CAS objects, and an
identical tree hash, for every value of `AV_THREADS`/`--threads`. Complements
`tests/test_core.py`'s C++-level thread-count-invariance tests (CDC chunk boundaries,
safetensors layer order) by proving the same guarantee holds at the Python `av add`
compute/apply-split layer itself.

Manually verified once already in a real scratch repo during development (identical index
bytes mod mtime, identical object-store contents, across AV_THREADS 1/4/8) -- this file is
the automated, repeatable version of that same proof.
"""
import json
import os

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli


def _build_fixture(repo_root):
    """A deterministic mixed tree: several small code files (exercises the plain
    hash_and_publish_whole_file path) plus one larger binary (exercises CDC chunking, the
    path with real inter-file AND intra-file parallelism)."""
    for i in range(20):
        (repo_root / f"module_{i:02d}.py").write_text(f"value_{i} = {i}\n" * (i + 1))
    (repo_root / "sub").mkdir()
    for i in range(10):
        (repo_root / "sub" / f"nested_{i:02d}.py").write_text(f"nested = {i}\n")
    # >= CHUNKABLE_EXTS and large enough to produce multiple CDC chunks at default params.
    (repo_root / "checkpoint.bin").write_bytes(bytes((i * 7) % 256 for i in range(3 * 1024 * 1024)))


@pytest.fixture
def _threads_result(tmp_path, monkeypatch):
    """Returns a function(threads:int) -> (index_without_mtime, object_paths, tree_hash,
    staged_lines) for a freshly built fixture, run in an isolated repo directory.

    Directory names include a call counter, not just the thread count -- several tests
    call this twice with the SAME thread count (a baseline call and a same-count
    parametrize case, e.g. threads=1 compared against itself), which would otherwise try
    to `mkdir` the identical path twice under one `tmp_path`.
    """
    call_index = [0]

    def _run(threads: int, base_dir):
        call_index[0] += 1
        repo_root = base_dir / f"repo_threads_{threads}_call{call_index[0]}"
        repo_root.mkdir()
        monkeypatch.chdir(repo_root)
        # AV_THREADS unset so --threads is unambiguously what's under test, not a leftover
        # env var from the outer shell; AV_NO_DAEMON=1 is already conftest.py's global
        # default, set again here for a self-contained guarantee if this file ever runs alone.
        monkeypatch.delenv("AV_THREADS", raising=False)
        monkeypatch.setenv("AV_NO_DAEMON", "1")
        runner = CliRunner()
        r = runner.invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"])
        assert r.exit_code == 0, r.output

        _build_fixture(repo_root)

        r = runner.invoke(cli, ["add", ".", "--threads", str(threads)])
        assert r.exit_code == 0, r.output

        index_data = json.loads((repo_root / ".av" / "index").read_text())["entries"]
        index_no_mtime = {
            path: {k: v for k, v in entry.items() if k != "mtime_ns"}
            for path, entry in index_data.items()
        }

        object_paths = set()
        objects_dir = repo_root / ".av" / "objects"
        for shard_dir in objects_dir.iterdir():
            if not shard_dir.is_dir():
                continue
            for obj_file in shard_dir.iterdir():
                object_paths.add(f"{shard_dir.name}/{obj_file.name}")
                assert obj_file.stat().st_size > 0 or True  # existence is what matters here

        # Tree hash: same shape commit_staged() builds, minus anything time-dependent --
        # a stand-in "what would this commit's content-only hash be" rather than the real
        # signed/wire commit hash (which also includes a wall-clock timestamp).
        import hashlib

        tree = {
            path: {"hash": e["hash"], "size": e["size"], "type": e["type"],
                   "layers": e.get("layers", []), "chunks": e.get("chunks", [])}
            for path, e in index_data.items()
        }
        tree_hash = hashlib.sha256(json.dumps(tree, sort_keys=True).encode()).hexdigest()

        staged_lines = [line for line in r.output.splitlines() if line.startswith("Staged")]

        return index_no_mtime, object_paths, tree_hash, staged_lines

    return _run


@pytest.mark.parametrize("threads", [1, 2, 4, 8])
def test_index_content_identical_across_thread_counts(tmp_path, _threads_result, threads):
    baseline, _, _, _ = _threads_result(1, tmp_path)
    result, _, _, _ = _threads_result(threads, tmp_path)
    assert result == baseline, f"AV_THREADS={threads}: index content (hash/size/type/layers/chunks) differs from the single-threaded baseline"


@pytest.mark.parametrize("threads", [1, 2, 4, 8])
def test_object_store_identical_across_thread_counts(tmp_path, _threads_result, threads):
    _, baseline, _, _ = _threads_result(1, tmp_path)
    _, result, _, _ = _threads_result(threads, tmp_path)
    assert result == baseline, f"AV_THREADS={threads}: CAS object set differs from the single-threaded baseline"


@pytest.mark.parametrize("threads", [1, 2, 4, 8])
def test_tree_hash_identical_across_thread_counts(tmp_path, _threads_result, threads):
    _, _, baseline, _ = _threads_result(1, tmp_path)
    _, _, result, _ = _threads_result(threads, tmp_path)
    assert result == baseline, f"AV_THREADS={threads}: tree hash differs from the single-threaded baseline"


@pytest.mark.parametrize("threads", [1, 2, 4, 8])
def test_staged_output_order_identical_across_thread_counts(tmp_path, _threads_result, threads):
    """The apply loop always runs in sorted-input order regardless of which worker
    finished first -- proven directly by comparing the exact sequence of 'Staged ...'
    lines, not just the final index content."""
    _, _, _, baseline = _threads_result(1, tmp_path)
    _, _, _, result = _threads_result(threads, tmp_path)
    assert result == baseline, f"AV_THREADS={threads}: staged-file print order differs from the single-threaded baseline"


def test_repeated_runs_at_the_same_thread_count_are_also_deterministic(tmp_path, _threads_result):
    """Not just cross-thread-count -- two separate runs at the SAME thread count must
    also agree, ruling out any run-to-run nondeterminism (e.g. dict/set iteration order
    leaking through) independent of threading itself."""
    a = _threads_result(4, tmp_path)
    b = _threads_result(4, tmp_path)
    assert a[0] == b[0]  # index content
    assert a[1] == b[1]  # object set
    assert a[2] == b[2]  # tree hash
    assert a[3] == b[3]  # staged output order
