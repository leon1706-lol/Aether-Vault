"""`av merge` tests: pure three-way/merge-base algorithms plus end-to-end CLI flows.

CLI tests run fully offline against an always-unreachable fake client (merges are local
operations; the push inside _finalize_commit degrades to the standard pending-push queue),
so these are fast and Docker-independent. Live-server parent round-tripping of merge
commits is covered in tests/test_server.py.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli
from python.av_cli import client as client_module
from python.av_cli.merge import (
    find_merge_base,
    summarize_changes,
    three_way_tree_merge,
    tree_is_flat,
)


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


class OfflineClient(client_module.VaultClient):
    """server_available()=False so every command stays purely local."""

    def __init__(self):
        super().__init__("http://offline")

    def server_available(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def offline_client(monkeypatch):
    monkeypatch.setattr(client_module, "VaultClient", lambda *a, **k: OfflineClient())


# ---------------------------------------------------------------------------
# pure algorithms
# ---------------------------------------------------------------------------

def entry(h, size=10):
    return {"hash": h, "size": size, "type": "code", "layers": []}


class TestThreeWayTreeMerge:
    def test_only_theirs_changed_takes_theirs(self):
        base = {"f.py": entry("a")}
        ours = {"f.py": entry("a")}
        theirs = {"f.py": entry("b")}
        merged, conflicts = three_way_tree_merge(base, ours, theirs)
        assert merged == {"f.py": entry("b")}
        assert conflicts == []

    def test_only_ours_changed_keeps_ours(self):
        merged, conflicts = three_way_tree_merge(
            {"f.py": entry("a")}, {"f.py": entry("c")}, {"f.py": entry("a")}
        )
        assert merged == {"f.py": entry("c")}
        assert conflicts == []

    def test_both_changed_identically_is_not_a_conflict(self):
        merged, conflicts = three_way_tree_merge(
            {"f.py": entry("a")}, {"f.py": entry("z")}, {"f.py": entry("z")}
        )
        assert merged == {"f.py": entry("z")}
        assert conflicts == []

    def test_both_changed_differently_conflicts(self):
        merged, conflicts = three_way_tree_merge(
            {"f.py": entry("a")}, {"f.py": entry("o")}, {"f.py": entry("t")}
        )
        assert merged == {}
        assert conflicts == ["f.py"]

    def test_delete_vs_untouched_takes_the_deletion(self):
        base = {"gone.py": entry("a"), "kept.py": entry("b")}
        ours = {"gone.py": entry("a"), "kept.py": entry("b")}          # ours untouched
        theirs = {"kept.py": entry("b")}                               # theirs deleted it
        merged, conflicts = three_way_tree_merge(base, ours, theirs)
        assert merged == {"kept.py": entry("b")}
        assert conflicts == []

        # reverse sides: ours deleted while theirs untouched → same result
        merged, conflicts = three_way_tree_merge(base, theirs, ours)
        assert merged == {"kept.py": entry("b")}
        assert conflicts == []

    def test_edit_vs_edit_of_different_files_merges_clean(self):
        base = {"x.py": entry("1"), "y.py": entry("2")}
        ours = {"x.py": entry("1-edited"), "y.py": entry("2")}
        theirs = {"x.py": entry("1"), "y.py": entry("2-edited")}
        merged, conflicts = three_way_tree_merge(base, ours, theirs)
        assert merged == {"x.py": entry("1-edited"), "y.py": entry("2-edited")}
        assert conflicts == []

    def test_modify_vs_delete_conflicts(self):
        base = {"f.py": entry("a")}
        ours = {"f.py": entry("mine")}   # modified
        theirs = {}                      # deleted
        merged, conflicts = three_way_tree_merge(base, ours, theirs)
        assert conflicts == ["f.py"]
        assert merged == {}

    def test_add_add_same_and_different(self):
        base = {}
        ours = {"new.py": entry("x")}
        theirs = {"new.py": entry("x"), "other.py": entry("y")}
        merged, conflicts = three_way_tree_merge(base, ours, theirs)
        assert merged == {"new.py": entry("x"), "other.py": entry("y")}
        assert conflicts == []

        theirs2 = {"new.py": entry("DIFFERENT")}
        _, conflicts = three_way_tree_merge(base, ours, theirs2)
        assert conflicts == ["new.py"]

    def test_layer_aware_equality_ignores_resplit_noise(self):
        """Same content re-split into identical layers compares equal via dict equality."""
        layered = {"hash": "whole", "size": 10, "type": "artifact",
                   "layers": [{"name": "l1", "hash": "aa", "size": 5}]}
        base = {"m.safetensors": layered}
        merged, conflicts = three_way_tree_merge(base, {"m.safetensors": layered},
                                                 {"m.safetensors": dict(layered)})
        assert conflicts == []
        assert merged["m.safetensors"]["layers"][0]["name"] == "l1"

    def test_summarize_changes(self):
        before = {"a": entry("1"), "b": entry("2"), "c": entry("3")}
        after = {"a": entry("1"), "b": entry("9"), "d": entry("4")}
        assert summarize_changes(before, after) == (1, 1, 1)


class TestFindMergeBase:
    def _loader(self, graph):
        return lambda h: graph.get(h)

    def test_linear_history(self):
        graph = {"a": {"parents": []}, "b": {"parents": ["a"]}, "c": {"parents": ["b"]}}
        load = self._loader(graph)
        assert find_merge_base(load, "c", "b") == "b"
        assert find_merge_base(load, "b", "c") == "b"
        assert find_merge_base(load, "c", "c") == "c"

    def test_forked_history_meets_at_common_root(self):
        graph = {
            "root": {"parents": []},
            "left": {"parents": ["root"]},
            "right": {"parents": ["root"]},
        }
        load = self._loader(graph)
        assert find_merge_base(load, "left", "right") == "root"

    def test_merge_commits_are_walked_through_every_parent(self):
        graph = {
            "root": {"parents": []},
            "l1": {"parents": ["root"]},
            "r1": {"parents": ["root"]},
            "m": {"parents": ["l1", "r1"]},      # merge node
            "tip": {"parents": ["m"]},
        }
        load = self._loader(graph)
        assert find_merge_base(load, "tip", "r1") == "r1"
        assert find_merge_base(load, "tip", "l1") == "l1"

    def test_unrelated_histories_return_none(self):
        graph = {"a": {"parents": []}, "b": {"parents": []}}
        assert find_merge_base(self._loader(graph), "a", "b") is None


def test_tree_is_flat_rejects_legacy_shape():
    assert tree_is_flat({"src/train.py": entry("h")}) is True
    assert tree_is_flat({"code": {}, "artifacts": {}}) is False


# ---------------------------------------------------------------------------
# CLI end-to-end (offline)
# ---------------------------------------------------------------------------

def _commit_file(repo: Path, name: str, content: str):
    (repo / name).write_text(content)
    invoke("add", name)


def _ref(repo: Path, branch="main"):
    return (repo / ".av" / "refs" / "heads" / branch).read_text().strip()


@pytest.fixture
def forked_repo(tmp_path, monkeypatch):
    """main + feature branched off it, each with their own follow-up commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    assert invoke("init", "--mode", "local", "--yes", "--no-repl").exit_code == 0

    _commit_file(repo, "shared.txt", "base")
    r = invoke("commit", "-m", "base")
    assert r.exit_code == 0, r.output
    base_hash = _ref(repo)

    assert invoke("branch", "feature").exit_code == 0
    assert invoke("checkout", "feature").exit_code == 0, r.output
    _commit_file(repo, "feature.txt", "feature work")
    r = invoke("commit", "-m", "feature change")
    assert r.exit_code == 0, r.output
    feature_hash = _ref(repo, "feature")

    assert invoke("checkout", "main").exit_code == 0
    _commit_file(repo, "main_only.txt", "main work")
    r = invoke("commit", "-m", "main change")
    assert r.exit_code == 0, r.output
    main_hash = _ref(repo)

    assert base_hash != feature_hash != main_hash
    return {"repo": repo, "base": base_hash, "main": main_hash, "feature": feature_hash}


def test_merge_fast_forwards_when_current_branch_is_strictly_behind(forked_repo):
    repo = forked_repo["repo"]
    base_hash = forked_repo["base"]

    # a branch parked exactly at the fork point (no commits of its own) is strictly behind
    # main → merging main into it must fast-forward, creating no merge commit
    assert invoke("checkout", base_hash[:7]).exit_code == 0
    assert invoke("branch", "ahead").exit_code == 0
    assert invoke("checkout", "ahead").exit_code == 0

    result = invoke("merge", "main")
    assert result.exit_code == 0, result.output
    assert "Fast-forwarded" in result.output
    assert _ref(repo, "ahead") == forked_repo["main"]
    assert (repo / "main_only.txt").read_text() == "main work"
    data = json.loads((repo / ".av" / "commits" / f"{_ref(repo, 'ahead')}.json").read_text())
    assert len(data["parents"]) <= 1
    # FF creates NO merge commit: feature tip == main tip exactly
    data = json.loads((repo / ".av" / "commits" / f"{_ref(repo, 'feature')}.json").read_text())
    assert len(data["parents"]) <= 1


def test_merge_creates_two_parent_commit_and_merges_trees(forked_repo):
    repo = forked_repo["repo"]
    result = invoke("merge", "feature")
    assert result.exit_code == 0, result.output
    assert "Merged feature into main" in result.output

    merged_hash = _ref(repo)
    data = json.loads((repo / ".av" / "commits" / f"{merged_hash}.json").read_text())
    assert data["parents"] == [forked_repo["main"], forked_repo["feature"]]
    assert set(data["tree"]) == {"shared.txt", "feature.txt", "main_only.txt"}

    # both sides' content present; clean status right after the merge
    assert (repo / "feature.txt").read_text() == "feature work"
    assert (repo / "main_only.txt").read_text() == "main work"
    status = invoke("status")
    assert "Nothing to commit" in status.output

    log_result = invoke("log")
    assert "Merge feature into main" in log_result.output


def test_merge_conflict_aborts_without_touching_anything(forked_repo):
    repo = forked_repo["repo"]

    # both branches edit shared.txt after the fork point
    assert invoke("checkout", "feature").exit_code == 0
    _commit_file(repo, "shared.txt", "feature's version")
    r = invoke("commit", "-m", "feature edits shared")
    assert r.exit_code == 0, r.output
    feature_tip = _ref(repo, "feature")

    assert invoke("checkout", "main").exit_code == 0
    _commit_file(repo, "shared.txt", "main's version")
    r = invoke("commit", "-m", "main edits shared")
    assert r.exit_code == 0, r.output
    main_before = _ref(repo)

    result = invoke("merge", "feature")
    # v1.2.5: a conflicting merge now honors the documented exit-code registry (14 =
    # merge_conflict) instead of exiting 0 — see AGENTS.md / the exit-code table fix.
    assert result.exit_code == 14, result.output
    assert "conflict" in result.output.lower()
    assert "shared.txt" in result.output
    assert "--ours" in result.output and "--theirs" in result.output

    # nothing was touched: ref, working file, and no stray merge commit
    assert _ref(repo) == main_before
    assert (repo / "shared.txt").read_text() == "main's version"
    commits_now = len(list((repo / ".av" / "commits").glob("*.json")))
    assert not any(
        json.loads(p.read_text()).get("parents") == [main_before, feature_tip]
        for p in (repo / ".av" / "commits").glob("*.json")
    )


def test_merge_conflict_resolved_with_theirs_flag(forked_repo):
    repo = forked_repo["repo"]
    assert invoke("checkout", "feature").exit_code == 0
    _commit_file(repo, "shared.txt", "feature's version")
    assert invoke("commit", "-m", "feature edits shared").exit_code == 0
    feature_tip = _ref(repo, "feature")

    assert invoke("checkout", "main").exit_code == 0
    _commit_file(repo, "shared.txt", "main's version")
    assert invoke("commit", "-m", "main edits shared").exit_code == 0

    result = invoke("merge", "feature", "--theirs")
    assert result.exit_code == 0, result.output
    assert "auto-resolved via --theirs" in result.output
    assert (repo / "shared.txt").read_text() == "feature's version"

    merged_hash = _ref(repo)
    data = json.loads((repo / ".av" / "commits" / f"{merged_hash}.json").read_text())
    assert data["tree"]["shared.txt"]["hash"] != forked_repo["base"]


def test_merge_refuses_dirty_tree(forked_repo):
    repo = forked_repo["repo"]
    (repo / "uncommitted.txt").write_text("dirty")
    invoke("add", "uncommitted.txt")

    result = invoke("merge", "feature")
    assert "uncommitted changes" in result.output
    assert (repo / "uncommitted.txt").exists()


def test_merge_already_up_to_date_and_detached(forked_repo):
    repo = forked_repo["repo"]
    result = invoke("merge", "main")   # merging main into main
    assert "Already up to date" in result.output

    tip = _ref(repo)
    r = invoke("checkout", tip[:7])
    assert r.exit_code == 0, r.output
    result = invoke("merge", "feature")
    assert "detached" in result.output


def test_merge_unknown_target(forked_repo):
    result = invoke("merge", "does-not-exist")
    assert "not found" in result.output
