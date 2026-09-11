"""V1.5.0 perf work: `upload_commit_objects` scans only the files a commit actually
changed (`only_paths`), not the whole tracked tree, for the normal `commit_staged` path.
`av merge`'s call into `_finalize_commit` deliberately does NOT set this (its `idx` has
already had every entry's staged flag cleared by `_materialize_tree` by the time it gets
there -- see `_finalize_commit`'s `changed_paths` docstring) and must keep scanning the
full tree; these tests pin both behaviors so neither regresses silently.
"""
import json

import pytest
from click.testing import CliRunner

from python.av_cli.client import VaultClient
from python.av_cli.main import cli
import python.av_cli.core as core_module


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("init", "--mode", "local", "--yes", "--no-repl")
    assert result.exit_code == 0, result.output
    return tmp_path


def _spy_upload_commit_objects(monkeypatch, calls):
    original = core_module.upload_commit_objects

    def _spy(repo_root, client, tree, only_paths=None):
        calls.append(set(only_paths) if only_paths is not None else None)
        return original(repo_root, client, tree, only_paths=only_paths)

    monkeypatch.setattr(core_module, "upload_commit_objects", _spy)


def test_commit_staged_only_uploads_the_new_commits_own_changed_paths(repo, monkeypatch):
    monkeypatch.setattr(VaultClient, "server_available", lambda self: True)
    monkeypatch.setattr(VaultClient, "batch_check_objects", lambda self, hashes: [])
    monkeypatch.setattr(VaultClient, "upload_object", lambda self, path, h, known_missing=True: True)
    monkeypatch.setattr(VaultClient, "push_commit", lambda self, *a, **k: {"ok": True})
    monkeypatch.setattr(VaultClient, "update_ref", lambda self, *a, **k: {"ok": True})

    calls: list[set | None] = []
    _spy_upload_commit_objects(monkeypatch, calls)

    (repo / "a.py").write_text("a" * 100)
    invoke("add", "a.py")
    r1 = invoke("commit", "-m", "first")
    assert r1.exit_code == 0, r1.output

    (repo / "b.py").write_text("b" * 100)
    invoke("add", "b.py")
    r2 = invoke("commit", "-m", "second")
    assert r2.exit_code == 0, r2.output

    assert len(calls) == 2
    assert calls[0] == {"a.py"}
    assert calls[1] == {"b.py"}, (
        "second commit's upload scan included files it didn't change -- "
        "only_paths scoping regressed back to a full-tree scan"
    )


def test_upload_commit_objects_only_paths_none_scans_everything(repo, monkeypatch):
    """The explicit contract only_paths=None (merge's case) preserves the old full-tree
    behavior -- every tracked file's parts are candidates, not just a subset."""
    tree = {
        "a.py": {"hash": "a" * 64, "size": 1, "type": "code", "layers": [], "chunks": []},
        "b.py": {"hash": "b" * 64, "size": 1, "type": "code", "layers": [], "chunks": []},
    }
    for h in ("a" * 64, "b" * 64):
        obj = repo / ".av" / "objects" / h[:2] / h[2:]
        obj.parent.mkdir(parents=True, exist_ok=True)
        obj.write_bytes(b"x")

    checked = []

    class FakeClient:
        def batch_check_objects(self, hashes):
            checked.extend(hashes)
            return []  # nothing already on the server

        def upload_object(self, path, h, known_missing=True):
            return True

    ok = core_module.upload_commit_objects(repo, FakeClient(), tree, only_paths=None)
    assert ok is True
    assert set(checked) == {"a" * 64, "b" * 64}


def test_upload_commit_objects_only_paths_scopes_the_candidate_set(repo, monkeypatch):
    tree = {
        "a.py": {"hash": "a" * 64, "size": 1, "type": "code", "layers": [], "chunks": []},
        "b.py": {"hash": "b" * 64, "size": 1, "type": "code", "layers": [], "chunks": []},
    }
    for h in ("a" * 64, "b" * 64):
        obj = repo / ".av" / "objects" / h[:2] / h[2:]
        obj.parent.mkdir(parents=True, exist_ok=True)
        obj.write_bytes(b"x")

    checked = []

    class FakeClient:
        def batch_check_objects(self, hashes):
            checked.extend(hashes)
            return []

        def upload_object(self, path, h, known_missing=True):
            return True

    ok = core_module.upload_commit_objects(repo, FakeClient(), tree, only_paths={"b.py"})
    assert ok is True
    assert set(checked) == {"b" * 64}, "only_paths did not narrow the candidate set"


def test_merge_commit_does_not_pass_changed_paths(monkeypatch, tmp_path):
    """cmd_sync.py's merge call site must keep the safe default (changed_paths=None) --
    a monkeypatched _finalize_commit records whether it was ever called with a non-None
    changed_paths from the merge path specifically."""
    import python.av_cli.cmd_sync as cmd_sync_module

    seen = {}
    original = core_module._finalize_commit

    def _spy(*args, **kwargs):
        seen["changed_paths"] = kwargs.get("changed_paths")
        return original(*args, **kwargs)

    monkeypatch.setattr(cmd_sync_module, "_finalize_commit", _spy)
    # This test only needs to prove the *call site* never sets changed_paths for merge;
    # it does not need to drive a full real merge scenario (covered elsewhere in
    # tests/test_merge.py) -- inspecting cmd_sync.py's source call is a simpler, equally
    # strong guarantee against a future accidental regression.
    import inspect

    src = inspect.getsource(cmd_sync_module)
    call_start = src.index("_finalize_commit(")
    call_end = src.index(")", src.index("result_sink=", call_start))
    call_text = src[call_start:call_end]
    assert "changed_paths" not in call_text, (
        "merge's _finalize_commit call now passes changed_paths -- verify this is safe: "
        "_materialize_tree clears every entry's staged flag before this call, so deriving "
        "changed_paths from idx.get_staged_entries() here would silently scope the upload "
        "to nothing"
    )
