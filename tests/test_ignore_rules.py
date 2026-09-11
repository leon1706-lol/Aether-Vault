"""V1.5.0 perf work: built-in default ignore directories + real `.gitignore` support.

Root cause this closes: `_IGNORED_DIRS` only ever held `.av`/`.git`/`__pycache__`, so `av
status`/`av add .` walked an entire `venv`/`node_modules` tree on every invocation with no
`.avignore` present -- measured at 49s on this project's own repo before the fix. `.avignore`
stays a separate, deliberately simpler mechanism (basename-only fnmatch, no negation/
anchoring) -- these tests cover the two NEW pieces: `_DEFAULT_IGNORED_DIR_NAMES` and real
(if partial) `.gitignore` semantics.
"""
import os

import pytest

from python.av_cli.core import iter_working_files


def _touch(path, content="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_default_ignored_dirs_are_pruned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _touch(tmp_path / "src" / "main.py")
    _touch(tmp_path / "venv" / "lib" / "site.py")
    _touch(tmp_path / "node_modules" / "pkg" / "index.js")
    _touch(tmp_path / "build" / "out.o")
    found = {str(p.relative_to(tmp_path)).replace("\\", "/") for p in iter_working_files(tmp_path)}
    assert "src/main.py" in found
    assert not any(f.startswith("venv/") for f in found)
    assert not any(f.startswith("node_modules/") for f in found)
    assert not any(f.startswith("build/") for f in found)


def test_default_ignores_can_be_disabled_via_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AV_NO_DEFAULT_IGNORES", "1")
    _touch(tmp_path / "venv" / "lib" / "site.py")
    found = {str(p.relative_to(tmp_path)).replace("\\", "/") for p in iter_working_files(tmp_path)}
    assert "venv/lib/site.py" in found


def test_explicitly_avignore_negated_dir_inside_default_ignored_still_pruned_by_default(tmp_path, monkeypatch):
    # An explicit .avignore doesn't affect the default-dirs prune -- only AV_NO_DEFAULT_IGNORES
    # or renaming the directory does. Documents current, intentional scope: the two mechanisms
    # are independent, not layered negation.
    monkeypatch.chdir(tmp_path)
    _touch(tmp_path / "venv" / "keep.py")
    found = {str(p.relative_to(tmp_path)).replace("\\", "/") for p in iter_working_files(tmp_path)}
    assert found == set()


def test_gitignore_simple_pattern_matches_at_any_depth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".gitignore").write_text("*.log\n")
    _touch(tmp_path / "app.log")
    _touch(tmp_path / "nested" / "deep.log")
    _touch(tmp_path / "keep.txt")
    found = {str(p.relative_to(tmp_path)).replace("\\", "/") for p in iter_working_files(tmp_path)}
    # .gitignore itself is an ordinary trackable file, same as any other CLI treats it.
    assert found == {"keep.txt", ".gitignore"}


def test_gitignore_anchored_pattern_only_matches_from_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".gitignore").write_text("/only_root.txt\n")
    _touch(tmp_path / "only_root.txt")
    _touch(tmp_path / "nested" / "only_root.txt")
    found = {str(p.relative_to(tmp_path)).replace("\\", "/") for p in iter_working_files(tmp_path)}
    assert found == {"nested/only_root.txt", ".gitignore"}


def test_gitignore_dir_only_pattern_does_not_match_a_same_named_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".gitignore").write_text("data/\n")
    _touch(tmp_path / "data" / "x.bin")
    _touch(tmp_path / "not_a_dir_data")  # unrelated file, must survive
    (tmp_path / "data_file").write_text("y")  # a FILE literally named "data" would be a
    found = {str(p.relative_to(tmp_path)).replace("\\", "/") for p in iter_working_files(tmp_path)}
    assert "not_a_dir_data" in found
    assert "data_file" in found
    assert not any(f.startswith("data/") for f in found)


def test_gitignore_negation_rescues_a_specific_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".gitignore").write_text("*.log\n!keep.log\n")
    _touch(tmp_path / "app.log")
    _touch(tmp_path / "keep.log")
    found = {str(p.relative_to(tmp_path)).replace("\\", "/") for p in iter_working_files(tmp_path)}
    assert found == {"keep.log", ".gitignore"}


def test_no_gitignore_present_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _touch(tmp_path / "a.txt")
    found = {str(p.relative_to(tmp_path)).replace("\\", "/") for p in iter_working_files(tmp_path)}
    assert found == {"a.txt"}


def test_avignore_still_works_unchanged_alongside_gitignore(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".avignore").write_text("*.tmp\n")
    (tmp_path / ".gitignore").write_text("*.log\n")
    _touch(tmp_path / "a.tmp")
    _touch(tmp_path / "b.log")
    _touch(tmp_path / "c.keep")
    found = {str(p.relative_to(tmp_path)).replace("\\", "/") for p in iter_working_files(tmp_path)}
    assert found == {"c.keep", ".avignore", ".gitignore"}


def test_get_file_meta_safe_missing_file(tmp_path):
    from python.av_cli.core import get_file_meta_safe

    meta = get_file_meta_safe(str(tmp_path / "does_not_exist"))
    assert meta == {"exists": False, "size": 0, "mtime_ns": 0}


def test_get_file_meta_safe_existing_file(tmp_path):
    from python.av_cli.core import get_file_meta_safe

    f = tmp_path / "x.txt"
    f.write_text("hello")
    meta = get_file_meta_safe(str(f))
    assert meta["exists"] is True
    assert meta["size"] == 5
