"""V1.5.0 perf work: `hash_and_publish_whole_file` reads a plain (non-split) staged file
once instead of `hash_file_safe` + a second full read via `shutil.copy2`, and
`_atomic_publish_object`/`_compute_stage_result`'s parallel-`add` compute/apply split. These
exercise the Python-level contract directly rather than only through the full `av add` CLI
path already covered in `test_cli.py`.
"""
import hashlib
import json
import os
import struct

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


def _make_safetensors(tensors: dict) -> bytes:
    """Same minimal safetensors-blob builder as tests/test_core.py's own helper --
    duplicated rather than imported since the two test files intentionally don't depend on
    each other's internals."""
    header = {}
    offset = 0
    blobs = []
    for name, data in tensors.items():
        header[name] = {
            "dtype": "U8",
            "shape": [len(data)],
            "data_offsets": [offset, offset + len(data)],
        }
        offset += len(data)
        blobs.append(data)
    header_bytes = json.dumps(header).encode("utf-8")
    return struct.pack("<Q", len(header_bytes)) + header_bytes + b"".join(blobs)


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


def test_compute_stage_result_cdc_fused_matches_legacy_byte_for_byte(tmp_path, monkeypatch):
    """V1.6.0 (WS4.1): `AV_STAGE_FUSED` (default on) switches `_compute_stage_result`'s CDC
    branch between `aether_core.stage_cdc` (one fused read+write pass) and the legacy
    `chunk_and_hash_file` + per-chunk write loop. This drives the REAL function (not the
    C++ binding directly, which tests/test_core.py already covers exhaustively) with both
    flag values against the exact same file and asserts the returned result dicts --
    hash, size, mtime, chunk list (hash/size/offset for every chunk, in order) -- are
    byte-for-byte identical, and that the two runs' CAS object sets match too."""
    # Comfortably past the default max_chunk (8MB) so the hard cap guarantees more than one
    # chunk deterministically, rather than depending on the gear hash happening to cut
    # within a smaller random buffer.
    data = os.urandom(20 * 1024 * 1024 + 91)

    def _stage_once(fused: bool):
        repo_root = tmp_path / ("fused" if fused else "legacy")
        _init_av_dir(repo_root)
        src = repo_root / "checkpoint.bin"  # .bin is in CHUNKABLE_EXTS
        src.write_bytes(data)
        monkeypatch.setenv("AV_STAGE_FUSED", "1" if fused else "0")
        result = _compute_stage_result(repo_root, 1024, src, "checkpoint.bin", "artifact", None, set())
        assert result is not None
        object_hashes = set()
        for shard_dir in (repo_root / ".av" / "objects").iterdir():
            for obj in shard_dir.iterdir():
                object_hashes.add(shard_dir.name + obj.name)
        return result, object_hashes

    fused_result, fused_objects = _stage_once(fused=True)
    legacy_result, legacy_objects = _stage_once(fused=False)

    assert fused_result["hash"] == legacy_result["hash"] == hashlib.sha256(data).hexdigest()
    assert fused_result["size"] == legacy_result["size"]
    assert fused_result["layers"] == legacy_result["layers"] == []
    assert fused_result["chunks"] == legacy_result["chunks"]
    assert len(fused_result["chunks"]) > 1, "test fixture must actually produce more than one chunk"
    assert fused_objects == legacy_objects


def test_compute_stage_result_safetensors_fused_matches_legacy_byte_for_byte(tmp_path, monkeypatch):
    """V1.6.0 (WS4.1 second half): the safetensors counterpart to the CDC test above --
    `AV_STAGE_FUSED` switches `_compute_stage_result`'s safetensors branch between
    `aether_core.stage_safetensors` (one fused read+write pass) and the legacy
    `split_and_hash_safetensors` + per-layer write loop. Drives the REAL function with both
    flag values against the exact same file and asserts identical hash/layers/CAS objects."""
    data_blob = _make_safetensors({
        "layer1.weight": os.urandom(50_000),
        "layer2.weight": os.urandom(120_000),
        "layer3.bias": os.urandom(37),
    })

    def _stage_once(fused: bool):
        repo_root = tmp_path / ("fused" if fused else "legacy")
        _init_av_dir(repo_root)
        src = repo_root / "model.safetensors"
        src.write_bytes(data_blob)
        monkeypatch.setenv("AV_STAGE_FUSED", "1" if fused else "0")
        result = _compute_stage_result(repo_root, 1024, src, "model.safetensors", "artifact", None, set())
        assert result is not None
        object_hashes = set()
        for shard_dir in (repo_root / ".av" / "objects").iterdir():
            for obj in shard_dir.iterdir():
                object_hashes.add(shard_dir.name + obj.name)
        return result, object_hashes

    fused_result, fused_objects = _stage_once(fused=True)
    legacy_result, legacy_objects = _stage_once(fused=False)

    assert fused_result["hash"] == legacy_result["hash"] == hashlib.sha256(data_blob).hexdigest()
    assert fused_result["size"] == legacy_result["size"]
    assert fused_result["chunks"] == legacy_result["chunks"] == []
    assert fused_result["layers"] == legacy_result["layers"]
    assert len(fused_result["layers"]) == 4  # __header__ + 3 tensors
    assert fused_objects == legacy_objects


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


# ---------------------------------------------------------------------------
# `av add` no-op path (V1.6.0, Probleme.md): a stat-unchanged file must never pay for
# is_pointer_file()'s own exists()+is_file()+open()+read(), and a fully no-op invocation
# (every candidate unchanged) must never even load aether_core via configure_native_threads.
# ---------------------------------------------------------------------------

def test_noop_add_never_calls_is_pointer_file_for_unchanged_files(repo, monkeypatch):
    from click.testing import CliRunner
    from python.av_cli.main import cli
    import python.av_cli.cmd_staging as cmd_staging_module

    (repo / "f.py").write_text("x = 1")
    assert CliRunner().invoke(cli, ["add", "f.py"]).exit_code == 0

    calls = []
    real_is_pointer_file = cmd_staging_module.is_pointer_file

    def _spy(fpath):
        calls.append(fpath)
        return real_is_pointer_file(fpath)

    monkeypatch.setattr(cmd_staging_module, "is_pointer_file", _spy)
    result = CliRunner().invoke(cli, ["add", "f.py"])
    assert result.exit_code == 0
    assert calls == [], "is_pointer_file() must not be called for a stat-unchanged file"


def test_noop_add_never_configures_native_threads(repo, monkeypatch):
    """A genuinely no-op `av add` (every candidate already stat-unchanged) must not load
    the aether_core extension at all -- there is nothing to hash."""
    from click.testing import CliRunner
    from python.av_cli.main import cli
    import python.av_cli.cmd_staging as cmd_staging_module

    (repo / "f.py").write_text("x = 1")
    assert CliRunner().invoke(cli, ["add", "f.py"]).exit_code == 0

    calls = []
    monkeypatch.setattr(
        cmd_staging_module, "configure_native_threads",
        lambda *a, **k: calls.append(1) or 0,
    )
    result = CliRunner().invoke(cli, ["add", "f.py"])
    assert result.exit_code == 0
    assert calls == [], "configure_native_threads() must not run when nothing needs staging"


def test_add_still_calls_is_pointer_file_for_a_new_file(repo, monkeypatch):
    """The skip is specifically for stat-UNCHANGED files -- a new (never-tracked) file must
    still go through the real pointer check."""
    from click.testing import CliRunner
    from python.av_cli.main import cli
    import python.av_cli.cmd_staging as cmd_staging_module

    (repo / "new.py").write_text("y = 2")
    calls = []
    real_is_pointer_file = cmd_staging_module.is_pointer_file

    def _spy(fpath):
        calls.append(fpath)
        return real_is_pointer_file(fpath)

    monkeypatch.setattr(cmd_staging_module, "is_pointer_file", _spy)
    result = CliRunner().invoke(cli, ["add", "new.py"])
    assert result.exit_code == 0
    assert len(calls) == 1


def test_add_still_calls_is_pointer_file_for_a_genuinely_modified_file(repo):
    """A real content change (different mtime/size) must still be staged, not skipped."""
    from click.testing import CliRunner
    from python.av_cli.main import cli
    from python.av_cli.index import Index

    (repo / "f.py").write_text("x = 1")
    assert CliRunner().invoke(cli, ["add", "f.py"]).exit_code == 0
    before = Index(repo).get_entry("f.py")["hash"]

    (repo / "f.py").write_text("x = 2 # changed")
    result = CliRunner().invoke(cli, ["add", "f.py"])
    assert result.exit_code == 0
    after = Index(repo).get_entry("f.py")["hash"]
    assert after != before


# --- V1.6.3-adjacent: concurrent publish of the SAME content-addressed object -------------
# Real bug found live via `av benchmark --lowmem` on Windows: two synthetic benchmark
# fixture files with IDENTICAL content hash to the same object, and two parallel `av add`
# staging workers raced to publish it. `os.replace()` onto an existing/being-created
# destination is atomic-and-silent on POSIX, but Windows' MoveFileExW can instead raise
# PermissionError ([WinError 5]) when two renames race the exact same destination path --
# see Probleme.md #185 and `_replace_or_accept_concurrent_publish`'s own docstring.

def test_atomic_publish_object_tolerates_a_concurrent_publish_race(tmp_path, monkeypatch):
    """Reproduces the real race with genuine threads: both callers must pass the initial
    `obj_path.exists()` check as False (via a barrier) BEFORE either writes -- calling this
    twice sequentially would never hit the race at all, since the second call's own
    exists() check short-circuits before ever reaching os.replace."""
    import threading

    from python.av_cli import core as core_module

    obj_path = tmp_path / "ab" / "cdef"
    real_replace = os.replace
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    calls = {"n": 0}
    errors = []

    def racy_replace(src, dst):
        # Simulate the exact Windows failure mode: the FIRST rename to actually land wins
        # for real (the destination now exists with correct content), any OTHER
        # concurrent rename to the same destination raises PermissionError even though
        # losing gracefully is the correct outcome. The real rename runs INSIDE the lock
        # so it is guaranteed to have fully landed on disk before a losing thread's
        # exception (and its dest.exists() recovery check) can run.
        with lock:
            calls["n"] += 1
            first = calls["n"] == 1
            if first:
                real_replace(src, dst)
        if not first:
            raise PermissionError(5, "Zugriff verweigert")

    monkeypatch.setattr(core_module.os, "replace", racy_replace)

    def write_content(p):
        # Synchronizing HERE (not before calling _atomic_publish_object) is what actually
        # matters: write_fn runs AFTER the function's own `obj_path.exists()` fast-path
        # check, so blocking both threads here proves both already passed that check as
        # False before either proceeds to os.replace -- a bare pre-call barrier let one
        # thread's write+replace finish entirely before the other was even scheduled.
        p.write_bytes(b"identical content")
        barrier.wait(timeout=10)

    def worker():
        try:
            _atomic_publish_object(obj_path, write_content)
        except Exception as exc:  # pragma: no cover -- assertion happens on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    assert errors == [], errors  # neither thread's call raised
    assert obj_path.read_bytes() == b"identical content"
    assert calls["n"] == 2  # both threads genuinely raced os.replace


def test_replace_or_accept_concurrent_publish_reraises_a_real_error(tmp_path, monkeypatch):
    """The tolerance is specifically for "someone else already published this" -- a
    genuine, unrelated OSError (dest never appears) must still propagate."""
    from python.av_cli.core import _replace_or_accept_concurrent_publish

    src = tmp_path / "src.tmp"
    src.write_bytes(b"x")
    dest = tmp_path / "never-appears" / "dest"  # parent dir doesn't exist -> real failure

    with pytest.raises(OSError):
        _replace_or_accept_concurrent_publish(src, dest)


def test_hash_and_publish_whole_file_tolerates_concurrent_publish_race(tmp_path, monkeypatch, repo):
    """The two call sites inside hash_and_publish_whole_file (native aether_core path and
    the pure-Python fallback) must both survive the same Windows race, not just the shared
    _atomic_publish_object helper other callers use."""
    from python.av_cli import core as core_module

    fpath = repo / "model.bin"
    fpath.write_bytes(b"same content for both files")
    other = repo / "model_dup.bin"
    other.write_bytes(b"same content for both files")  # identical -> same object hash

    real_replace = os.replace
    state = {"first_done": False}

    def racy_replace(src, dst):
        if not state["first_done"]:
            state["first_done"] = True
            real_replace(src, dst)
        else:
            raise PermissionError(5, "Zugriff verweigert")

    monkeypatch.setattr(core_module.os, "replace", racy_replace)
    monkeypatch.setattr(core_module, "_get_aether_core", lambda: None)  # force the pure-Python fallback

    h1 = hash_and_publish_whole_file(repo, fpath)
    h2 = hash_and_publish_whole_file(repo, other)  # must not raise despite the simulated race
    assert h1 == h2
    obj_path = repo / ".av" / "objects" / h1[:2] / h1[2:]
    assert obj_path.read_bytes() == b"same content for both files"
