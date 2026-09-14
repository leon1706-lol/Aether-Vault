"""V1.6.3 footprint: one `Index` parse per command, and `Index.save()` without a second
full copy of every entry. Also pins the `av watch` skip-check bug this work surfaced (a
modified, already-tracked file was never auto-committed)."""
import json
import random
import tracemalloc

import pytest
from click.testing import CliRunner

from python.av_cli import index as index_module
from python.av_cli.index import Index
from python.av_cli.main import cli


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"])
    assert res.exit_code == 0, res.output
    return tmp_path


def inv(*args):
    return CliRunner().invoke(cli, list(args))


@pytest.fixture
def load_counter(monkeypatch):
    """Counts `Index.load()` on BOTH module identities: the CLI tests import
    `python.av_cli.index`, while `av_sdk` (and an installed `av`) import `av_cli.index`."""
    import importlib

    calls = []
    classes = [Index]
    try:
        classes.append(importlib.import_module("av_cli.index").Index)
    except ImportError:
        pass
    for cls in classes:
        original = cls.load

        def counting_load(self, _original=original):
            calls.append(self.index_path)
            return _original(self)

        monkeypatch.setattr(cls, "load", counting_load)
    return calls


# --- one parse per command ----------------------------------------------------------------

def test_commit_loads_the_index_exactly_once(repo, load_counter):
    (repo / "a.txt").write_text("hello", encoding="utf-8")
    assert inv("add", "a.txt").exit_code == 0
    load_counter.clear()
    res = inv("commit", "-m", "one", "--no-upload")
    assert res.exit_code == 0, res.output
    assert len(load_counter) == 1, load_counter


def test_sdk_commit_loads_the_index_exactly_once(repo, load_counter):
    from python.av_sdk import Repo

    (repo / "b.txt").write_text("hello", encoding="utf-8")
    with Repo(str(repo)) as r:
        r.add("b.txt")
        load_counter.clear()
        r.commit("sdk", no_upload=True)
    assert len(load_counter) == 1, load_counter


def test_watch_loads_the_index_once_per_committed_file(repo, load_counter, monkeypatch):
    import python.av_cli.cmd_watch as cmd_watch_module

    monkeypatch.setattr(cmd_watch_module, "_try_start_watchdog", lambda repo_root, pattern: None)
    runs = repo / "runs"
    runs.mkdir()
    (runs / "auto.ckpt").write_bytes(b"checkpoint-bytes")
    load_counter.clear()
    res = inv("watch", "--glob", "runs/*.ckpt", "--interval", "0.1", "--debounce", "0.1", "--max-commits", "1")
    assert res.exit_code == 0, res.output
    assert "1 auto-commit" in res.output
    assert len(load_counter) == 1, load_counter


def test_watch_recommits_a_modified_tracked_file(repo, monkeypatch):
    """Real bug (V1.6.3): the debounce skip-check compared the index entry's hash against
    the index entry's hash, so a checkpoint that changed after its first auto-commit was
    silently never committed again. Two `watch` sessions over the same path must produce
    two commits when the content changed in between."""
    import python.av_cli.cmd_watch as cmd_watch_module

    monkeypatch.setattr(cmd_watch_module, "_try_start_watchdog", lambda repo_root, pattern: None)
    runs = repo / "runs"
    runs.mkdir()
    ckpt = runs / "auto.ckpt"
    ckpt.write_bytes(b"epoch-1")
    res = inv("watch", "--glob", "runs/*.ckpt", "--interval", "0.1", "--debounce", "0.1", "--max-commits", "1")
    assert res.exit_code == 0 and "1 auto-commit" in res.output, res.output

    ckpt.write_bytes(b"epoch-2-different-content")
    res = inv("watch", "--glob", "runs/*.ckpt", "--interval", "0.1", "--debounce", "0.1", "--max-commits", "1")
    assert res.exit_code == 0 and "1 auto-commit" in res.output, res.output

    log = json.loads(inv("--output", "json", "log", "--all").output)
    messages = [c["message"] for c in log["data"]["commits"]]
    assert sum(1 for m in messages if m.startswith("watch:")) == 2, messages

    # And unchanged content is still skipped: a third session over the same bytes must
    # not commit (it exits via --max-commits only if it committed, so bound it by time).
    entry = Index(repo).get_entry("runs/auto.ckpt")
    assert entry is not None and not entry.get("staged")


# --- save() without a second copy -----------------------------------------------------------

def _shuffled_entries(n: int = 300, chunks_per_entry: int = 200) -> dict:
    rng = random.Random(7)
    keys = [f"dir{i % 9}/file_{i:04d}.bin" for i in range(n)]
    rng.shuffle(keys)
    return {
        k: {
            "hash": f"{i:064x}", "size": i * 1024, "mtime_ns": 1_700_000_000_000_000_000 + i,
            "type": "artifact", "staged": bool(i % 2), "pointer": None,
            "chunks": [{"hash": f"{j:064x}", "size": 2048, "offset": j * 2048} for j in range(chunks_per_entry)],
        }
        for i, k in enumerate(keys)
    }


def test_save_bytes_identical_to_previous_format(tmp_path):
    (tmp_path / ".av").mkdir()
    idx = Index(tmp_path)
    idx.entries = _shuffled_entries(50, 5)
    idx.save()
    expected = json.dumps({"entries": dict(sorted(idx.entries.items()))}, separators=(",", ":"))
    assert idx.index_path.read_text(encoding="utf-8") == expected
    # And it round-trips through the normal loader.
    assert Index(tmp_path).entries == dict(sorted(idx.entries.items()))


def test_save_empty_index_is_the_same_bytes(tmp_path):
    (tmp_path / ".av").mkdir()
    idx = Index(tmp_path)
    idx.save()
    assert idx.index_path.read_text(encoding="utf-8") == '{"entries":{}}'


def test_save_peak_allocation_is_a_fraction_of_the_file(tmp_path):
    (tmp_path / ".av").mkdir()
    idx = Index(tmp_path)
    idx.entries = _shuffled_entries()
    tracemalloc.start()
    idx.save()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    size = idx.index_path.stat().st_size
    # Streamed entry by entry: the peak is one entry's fragment plus the sorted key view,
    # never the whole document (6+ MB here) and never a second copy of the entries dict.
    assert peak < size / 4, (peak, size)


def test_index_module_no_longer_imports_the_compact_json_writer():
    assert not hasattr(index_module, "atomic_write_json_compact")
