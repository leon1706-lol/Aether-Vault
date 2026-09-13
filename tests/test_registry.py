from python.av_cli.cmd_registry import _collect_exportable_object_hashes
from python.av_cli.main import load_registry, update_registry, load_config, save_config


def test_load_registry_defaults_when_missing(repo):
    reg = load_registry(repo)
    assert reg == {"tags": [], "metrics": []}


def test_update_registry_merges_and_dedupes(repo):
    update_registry(repo, ["v1"], {"sharpe": 1.5})
    update_registry(repo, ["v1", "v2"], {"sharpe": 2.0, "drawdown": 0.1})

    reg = load_registry(repo)
    assert reg["tags"] == ["v1", "v2"]
    assert reg["metrics"] == ["drawdown", "sharpe"]


def test_load_config_backfills_project_id(repo):
    cfg_path = repo / ".av" / "config"
    cfg_path.write_text('{"lfs_threshold_mb": 50, "remote_url": "http://localhost:8000"}')

    cfg = load_config(repo)
    assert "project_id" in cfg
    assert cfg["project_name"] == repo.name

    # Backfill must persist — a second load shouldn't generate a *different* project_id.
    cfg2 = load_config(repo)
    assert cfg2["project_id"] == cfg["project_id"]


def test_save_config_atomic_no_tmp_file_left_behind(repo):
    cfg = load_config(repo)
    save_config(repo, cfg)

    leftovers = list((repo / ".av").glob("*.tmp.*"))
    assert leftovers == []


# ---------------------------------------------------------------------------
# _collect_exportable_object_hashes — V1.6.0 real-bug fix (Probleme.md): a layer-split/
# CDC-chunked entry's whole-file hash was never uploaded, so requesting it during export
# always 404'd and inflated the "failed" count for every project with split artifacts.
# ---------------------------------------------------------------------------

def test_collect_exportable_object_hashes_whole_file_entry():
    commits = [{"tree": {"a.py": {"hash": "deadbeef" * 8}}}]
    assert _collect_exportable_object_hashes(commits) == {"deadbeef" * 8}


def test_collect_exportable_object_hashes_excludes_whole_hash_for_layered_entry():
    commits = [{"tree": {"model.safetensors": {
        "hash": "wholehash" * 7 + "wh",  # never uploaded -- must not be requested
        "layers": [{"hash": "layer1hash" * 6 + "l1"}, {"hash": "layer2hash" * 6 + "l2"}],
    }}}]
    hashes = _collect_exportable_object_hashes(commits)
    assert "wholehash" * 7 + "wh" not in hashes
    assert {"layer1hash" * 6 + "l1", "layer2hash" * 6 + "l2"} == hashes


def test_collect_exportable_object_hashes_excludes_whole_hash_for_chunked_entry():
    commits = [{"tree": {"checkpoint.pt": {
        "hash": "wholehash" * 7 + "wh",  # never uploaded -- must not be requested
        "chunks": [{"hash": "chunk1hash" * 6 + "c1"}, {"hash": "chunk2hash" * 6 + "c2"}],
    }}}]
    hashes = _collect_exportable_object_hashes(commits)
    assert "wholehash" * 7 + "wh" not in hashes
    assert {"chunk1hash" * 6 + "c1", "chunk2hash" * 6 + "c2"} == hashes


def test_collect_exportable_object_hashes_across_multiple_commits_dedupes():
    shared_hash = "shared0" * 9 + "sh"
    commits = [
        {"tree": {"a.py": {"hash": shared_hash}}},
        {"tree": {"b.py": {"hash": shared_hash}}},
    ]
    assert _collect_exportable_object_hashes(commits) == {shared_hash}


def test_collect_exportable_object_hashes_handles_missing_tree_key():
    assert _collect_exportable_object_hashes([{"hash": "x"}, {}]) == set()
