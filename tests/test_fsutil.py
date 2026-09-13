import json

import pytest

from python.av_cli.fsutil import atomic_write_json, atomic_write_text


def test_atomic_write_text_creates_file_with_content(tmp_path):
    path = tmp_path / "out.txt"
    atomic_write_text(path, "hello world")
    assert path.read_text(encoding="utf-8") == "hello world"


def test_atomic_write_text_fully_overwrites_not_appends(tmp_path):
    path = tmp_path / "out.txt"
    atomic_write_text(path, "first version, much longer than the second")
    atomic_write_text(path, "short")
    assert path.read_text(encoding="utf-8") == "short"


def test_atomic_write_text_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "dirs" / "out.txt"
    atomic_write_text(path, "content")
    assert path.read_text(encoding="utf-8") == "content"


def test_atomic_write_text_does_not_leave_a_temp_file_behind(tmp_path):
    path = tmp_path / "out.txt"
    atomic_write_text(path, "content")
    leftovers = [p for p in tmp_path.iterdir() if p.name != "out.txt"]
    assert leftovers == []


def test_atomic_write_text_leaves_original_intact_if_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "out.txt"
    atomic_write_text(path, "original content")

    import python.av_cli.fsutil as fsutil_module

    def _boom(*args, **kwargs):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(fsutil_module.os, "replace", _boom)

    with pytest.raises(OSError):
        atomic_write_text(path, "new content that should never land")

    # The original file must be untouched — os.replace never ran, so the old content survives.
    assert path.read_text(encoding="utf-8") == "original content"
    # The temp file's own cleanup (the `finally` block) must still have removed it, not leaked.
    leftovers = [p for p in tmp_path.iterdir() if p.name != "out.txt"]
    assert leftovers == []


def test_atomic_write_json_round_trips_data(tmp_path):
    path = tmp_path / "out.json"
    data = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}
    atomic_write_json(path, data)
    assert json.loads(path.read_text(encoding="utf-8")) == data


# ---------------------------------------------------------------------------
# WS4.8: `durable=False` skips the fsync (a clone can always be redone) while staying
# atomic -- readers never see a truncated/half-written file either way.
# ---------------------------------------------------------------------------

def test_durable_true_is_still_the_default_and_calls_fsync(tmp_path, monkeypatch):
    import python.av_cli.fsutil as fsutil_module

    calls = []
    monkeypatch.setattr(fsutil_module.os, "fsync", lambda fd: calls.append(fd))
    atomic_write_text(tmp_path / "out.txt", "content")
    assert len(calls) == 1


def test_durable_false_skips_fsync_but_still_writes_correct_content(tmp_path, monkeypatch):
    import python.av_cli.fsutil as fsutil_module

    calls = []
    monkeypatch.setattr(fsutil_module.os, "fsync", lambda fd: calls.append(fd))
    path = tmp_path / "out.txt"
    atomic_write_text(path, "content", durable=False)
    assert calls == []
    assert path.read_text(encoding="utf-8") == "content"


def test_durable_false_is_still_atomic_no_temp_file_left_behind(tmp_path):
    path = tmp_path / "out.txt"
    atomic_write_text(path, "content", durable=False)
    leftovers = [p for p in tmp_path.iterdir() if p.name != "out.txt"]
    assert leftovers == []


def test_atomic_write_json_durable_false_threads_through_to_atomic_write_text(tmp_path, monkeypatch):
    import python.av_cli.fsutil as fsutil_module

    calls = []
    monkeypatch.setattr(fsutil_module.os, "fsync", lambda fd: calls.append(fd))
    path = tmp_path / "out.json"
    data = {"a": 1}
    atomic_write_json(path, data, durable=False)
    assert calls == []
    assert json.loads(path.read_text(encoding="utf-8")) == data
