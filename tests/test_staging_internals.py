"""V1.5.0 perf work: `hash_and_publish_whole_file` reads a plain (non-split) staged file
once instead of `hash_file_safe` + a second full read via `shutil.copy2`, and
`_atomic_publish_object`/`_compute_stage_result`'s parallel-`add` compute/apply split. These
exercise the Python-level contract directly rather than only through the full `av add` CLI
path already covered in `test_cli.py`.
"""
import hashlib

import pytest

from python.av_cli.core import (
    _atomic_publish_object,
    _compute_stage_result,
    apply_stage_result,
    hash_and_publish_whole_file,
    hash_file_safe,
)
from python.av_cli.index import Index


def _init_av_dir(repo_root):
    (repo_root / ".av" / "objects").mkdir(parents=True)


def test_hash_and_publish_whole_file_matches_hash_file_safe(tmp_path):
    _init_av_dir(tmp_path)
    src = tmp_path / "f.txt"
    src.write_text("hello world" * 500)
    h = hash_and_publish_whole_file(tmp_path, src)
    assert h == hashlib.sha256(src.read_bytes()).hexdigest()
    obj_path = tmp_path / ".av" / "objects" / h[:2] / h[2:]
    assert obj_path.exists()
    assert obj_path.read_bytes() == src.read_bytes()


def test_hash_and_publish_whole_file_skips_write_when_object_already_exists(tmp_path):
    _init_av_dir(tmp_path)
    src = tmp_path / "f.txt"
    src.write_text("duplicate content")
    h1 = hash_and_publish_whole_file(tmp_path, src)
    obj_path = tmp_path / ".av" / "objects" / h1[:2] / h1[2:]
    marker = obj_path.stat().st_mtime_ns

    src2 = tmp_path / "g.txt"
    src2.write_text("duplicate content")  # identical bytes -> same hash
    h2 = hash_and_publish_whole_file(tmp_path, src2)
    assert h2 == h1
    # The object was NOT rewritten a second time -- its mtime is untouched.
    assert obj_path.stat().st_mtime_ns == marker
    # No leftover scratch temp files.
    leftovers = list((tmp_path / ".av" / "objects").glob(".stage-tmp.*"))
    assert leftovers == []


def test_hash_and_publish_whole_file_no_leftover_temp_on_success(tmp_path):
    _init_av_dir(tmp_path)
    src = tmp_path / "f.txt"
    src.write_text("x" * 10000)
    hash_and_publish_whole_file(tmp_path, src)
    leftovers = list((tmp_path / ".av" / "objects").glob("**/.stage-tmp.*"))
    assert leftovers == []


def test_atomic_publish_object_writes_once_and_skips_if_exists(tmp_path):
    obj_path = tmp_path / "ab" / "cdef"
    calls = []

    def _write(tmp):
        calls.append(1)
        tmp.write_text("payload")

    _atomic_publish_object(obj_path, _write)
    assert obj_path.read_text() == "payload"
    assert len(calls) == 1

    _atomic_publish_object(obj_path, _write)  # second call: object already exists
    assert len(calls) == 1  # write_fn not called again
    assert obj_path.read_text() == "payload"


def test_atomic_publish_object_cleans_up_temp_on_failure(tmp_path):
    obj_path = tmp_path / "ab" / "cdef"

    def _write(tmp):
        tmp.write_text("partial")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _atomic_publish_object(obj_path, _write)
    assert not obj_path.exists()
    leftovers = list((tmp_path / "ab").glob("cdef.tmp.*"))
    assert leftovers == []


def test_compute_and_apply_stage_result_roundtrip(tmp_path):
    repo_root = tmp_path
    _init_av_dir(repo_root)
    (repo_root / ".av" / "config").write_text('{"lfs_threshold_mb": 50}')
    src = repo_root / "small.py"
    src.write_text("print('hi')")

    idx = Index(repo_root)
    result = _compute_stage_result(
        repo_root, 50 * 1024 * 1024, src, "small.py", "code", None, set()
    )
    assert result is not None
    assert result["hash"] == hash_file_safe(str(src))
    apply_stage_result(idx, result)
    entry = idx.get_entry("small.py")
    assert entry["hash"] == result["hash"]
    assert entry["staged"] is True


def test_compute_stage_result_returns_none_when_unchanged(tmp_path):
    repo_root = tmp_path
    _init_av_dir(repo_root)
    src = repo_root / "small.py"
    src.write_text("print('hi')")
    meta_existing = {
        "hash": "deadbeef", "size": src.stat().st_size, "mtime_ns": src.stat().st_mtime_ns,
        "type": "code", "staged": False, "pointer": None,
    }
    result = _compute_stage_result(
        repo_root, 50 * 1024 * 1024, src, "small.py", "code", meta_existing, set()
    )
    assert result is None
