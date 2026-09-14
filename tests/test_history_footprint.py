"""V1.6.3: `av log --all` scans commit files without holding every tree, and retains only
the `limit` newest entries while scanning."""
import json
import tracemalloc

from click.testing import CliRunner

from python.av_cli import history
from python.av_cli.main import cli


def _write_commits(repo_root, count: int, tree_entries: int) -> None:
    commits_dir = repo_root / ".av" / "commits"
    commits_dir.mkdir(parents=True, exist_ok=True)
    tree = {f"weights/layer_{i}.bin": {"hash": f"{i:064x}", "size": 1024, "type": "artifact",
                                        "layers": [], "chunks": [{"hash": f"{j:064x}", "size": 64, "offset": j * 64}
                                                                 for j in range(20)]}
            for i in range(tree_entries)}
    for n in range(count):
        h = f"{n:064x}"
        (commits_dir / f"{h}.json").write_text(json.dumps({
            "hash": h, "message": f"commit {n}", "timestamp": f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}",
            "parents": [f"{n - 1:064x}"] if n else [], "tags": [], "metrics": {}, "tree": tree,
        }), encoding="utf-8")


def test_collect_all_commits_retains_only_limit_and_drops_tree(tmp_path):
    _write_commits(tmp_path, count=200, tree_entries=500)
    tracemalloc.start()
    commits = history.collect_all_commits(tmp_path, 30)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(commits) == 30
    assert all("tree" not in c for c in commits)
    assert [c["message"] for c in commits[:3]] == ["commit 199", "commit 198", "commit 197"]
    # One 500-entry tree (with chunk lists) parses to well over a megabyte; holding all
    # 200 would be hundreds of MB. Peak must stay in the single-file-being-parsed range.
    one_tree_bytes = len(json.dumps(json.loads((tmp_path / ".av" / "commits" / f"{0:064x}.json").read_text())["tree"]))
    assert peak < 6 * one_tree_bytes, (peak, one_tree_bytes)


def test_collect_all_commits_tolerates_corrupt_files_and_zero_limit(tmp_path):
    _write_commits(tmp_path, count=3, tree_entries=1)
    (tmp_path / ".av" / "commits" / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / ".av" / "commits" / "list.json").write_text("[1, 2]", encoding="utf-8")
    assert len(history.collect_all_commits(tmp_path, 10)) == 3
    assert history.collect_all_commits(tmp_path, 0) == []


def test_load_commit_meta_drops_tree_only():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "c.json"
        p.write_text(json.dumps({"hash": "abc", "tree": {"a": 1}, "message": "m"}), encoding="utf-8")
        assert history.load_commit_meta(p) == {"hash": "abc", "message": "m"}
        p.write_text("nope", encoding="utf-8")
        assert history.load_commit_meta(p) is None


def test_log_all_cli_still_renders_newest_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"]).exit_code == 0
    for i in range(3):
        (tmp_path / "f.txt").write_text(f"v{i}", encoding="utf-8")
        assert runner.invoke(cli, ["add", "f.txt"]).exit_code == 0
        assert runner.invoke(cli, ["commit", "-m", f"c{i}", "--no-upload"]).exit_code == 0
    out = runner.invoke(cli, ["--output", "json", "log", "--all", "--limit", "2"])
    assert out.exit_code == 0, out.output
    commits = json.loads(out.output)["data"]["commits"]
    assert [c["message"] for c in commits] == ["c2", "c1"]
    assert all("tree" not in c for c in commits)
