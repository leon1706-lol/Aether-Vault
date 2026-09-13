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


def test_explicit_repo_root_is_used_instead_of_cwd_based_resolution(tmp_path, monkeypatch):
    """V1.6.0 (Probleme.md real bug): `iter_working_files()` used to always resolve the
    ignore-rule context via `find_repo_root()` walking up from the *process's* CWD, never
    from its own `root` argument -- exactly what happened to `speedcheck.py`'s synthetic
    benchmarks (CWD = wherever the benchmark was invoked from, `root` = a disposable fixture
    directory). Note this is NOT a *correctness* bug when `root` sits outside the CWD-found
    repo (`_rel_posix()` catches the resulting `ValueError` and simply skips gitignore
    matching for that path) -- it's a *cost* bug: `find_repo_root()` runs at all, and every
    single walked entry pays a `Path.relative_to()` call that raises and is caught, instead
    of the whole ignore-context resolution being skipped outright. That per-file exception
    overhead, multiplied over a couple thousand files, is what inflated a measured "9x
    regression" that had nothing to do with the walk algorithm itself. This test pins down
    the actual mechanism of the fix: `find_repo_root()` must not even be called when the
    caller supplies `repo_root=` explicitly."""
    cwd_repo = tmp_path / "cwd-repo"
    (cwd_repo / ".av").mkdir(parents=True)  # find_repo_root() only recognizes an .av marker
    (cwd_repo / ".gitignore").write_text("*.secret\n")
    monkeypatch.chdir(cwd_repo)

    target_root = tmp_path / "target-repo"
    _touch(target_root / "model.secret")

    import python.av_cli.core as core_module

    calls = []
    real_find_repo_root = core_module.find_repo_root

    def _spy():
        calls.append(1)
        return real_find_repo_root()

    monkeypatch.setattr(core_module, "find_repo_root", _spy)

    # Without repo_root=: falls back to find_repo_root() from CWD.
    list(iter_working_files(target_root))
    assert calls == [1], "find_repo_root() must be called exactly once when repo_root is omitted"

    calls.clear()

    # With repo_root=target_root: find_repo_root() must not run at all -- the explicit
    # value is used as-is, avoiding both the CWD walk-up and the per-file relative_to()
    # exception cost for paths outside whatever it would have found.
    found_explicit = {str(p.relative_to(target_root)).replace("\\", "/")
                      for p in iter_working_files(target_root, repo_root=target_root)}
    assert calls == [], "find_repo_root() must not be called when repo_root is given explicitly"
    assert "model.secret" in found_explicit  # target_root has no .gitignore of its own


def test_explicit_repo_root_is_used_for_avignore_too(tmp_path, monkeypatch):
    cwd_repo = tmp_path / "cwd-repo"
    cwd_repo.mkdir()
    (cwd_repo / ".avignore").write_text("*.dat\n")
    monkeypatch.chdir(cwd_repo)

    target_root = tmp_path / "target-repo"
    _touch(target_root / "weights.dat")

    found = {str(p.relative_to(target_root)).replace("\\", "/")
             for p in iter_working_files(target_root, repo_root=target_root)}
    assert "weights.dat" in found


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
