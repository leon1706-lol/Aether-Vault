import os
import json
import pytest
from pathlib import Path

from python.av_cli.pointer import create_pointer, parse_pointer, is_pointer_file, get_pointer_path
from python.av_cli.index import Index
from python.av_cli.handoff import classify_lineage, diff_model_weights, generate_handoff

def test_create_and_parse_pointer(tmp_path):
    p = tmp_path / "model.pt"
    p.write_text("dummy content")

    # create_pointer enforces a canonical 64-char hex SHA-256 (content-addressing
    # invariant), so the fixture must use a valid digest, not a placeholder string.
    valid_hash = "a" * 64
    ptr_content = create_pointer(p, valid_hash, 1024)
    assert valid_hash in ptr_content
    assert "1024" in ptr_content

    parsed = parse_pointer(ptr_content)
    assert parsed["hash"] == valid_hash
    assert parsed["size"] == 1024
    assert parsed["original_path"] == "model.pt"

def test_is_pointer_file(tmp_path):
    p = tmp_path / "model.pt.av-pointer"
    p.write_text("version aether-vault-pointer v1\nhash-sha256 fakehash\nsize 10\n")
    assert is_pointer_file(p)
    
    p2 = tmp_path / "normal.txt"
    p2.write_text("hello world")
    assert not is_pointer_file(p2)

def test_index_operations(tmp_path):
    idx = Index(tmp_path)
    idx.add_entry("model.pt", "hash123", 100, 200, "artifact")
    assert "model.pt" in idx.entries
    assert idx.get_entry("model.pt")["hash"] == "hash123"
    assert idx.get_staged_entries()["model.pt"]["staged"] is True
    
    idx.clear_staged()
    assert idx.get_staged_entries() == {}

    # Re-adding the same content (same hash) must not re-stage it —
    # otherwise `add .` after a commit lets you commit again with no real change.
    idx.add_entry("model.pt", "hash123", 100, 200, "artifact")
    assert idx.get_staged_entries() == {}

    # A real content change (different hash) must re-stage it.
    idx.add_entry("model.pt", "hash456", 100, 200, "artifact")
    assert idx.get_staged_entries()["model.pt"]["staged"] is True

    idx.remove_entry("model.pt")
    assert "model.pt" not in idx.entries


def test_classify_lineage():
    assert classify_lineage("weights/epoch_50.safetensors") == "model"
    assert classify_lineage("model.pt") == "model"
    assert classify_lineage("data/train.parquet") == "dataset"
    assert classify_lineage("data/labels.csv") == "dataset"
    assert classify_lineage("src/train.py") == "code"


def test_diff_model_weights_layers():
    current_tree = [{
        "rel_path": "weights/model.safetensors",
        "hash": "newhash",
        "size": 100,
        "layers": [
            {"name": "layer1", "hash": "a1"},
            {"name": "layer2", "hash": "b2-changed"},
        ],
    }]
    parent_commit_hash = "parent123"

    def fake_load_commit(repo_root, commit_hash):
        assert commit_hash == parent_commit_hash
        return {
            "tree": {
                "weights/model.safetensors": {
                    "hash": "oldhash",
                    "layers": [
                        {"name": "layer1", "hash": "a1"},
                        {"name": "layer2", "hash": "b2-original"},
                    ],
                }
            }
        }

    import python.av_cli.handoff as handoff_module
    orig = handoff_module.load_commit
    handoff_module.load_commit = fake_load_commit
    try:
        result = diff_model_weights(Path("."), current_tree, parent_commit_hash)
    finally:
        handoff_module.load_commit = orig

    assert len(result) == 1
    assert result[0]["status"] == "changed"
    assert result[0]["changed_layers"] == ["layer2"]
    assert result[0]["unchanged_layers"] == 1


def _init_fake_repo(repo_root: Path) -> str:
    """Sets up a minimal .av/ structure with one commit on `main`, returns the commit hash."""
    (repo_root / ".av" / "commits").mkdir(parents=True)
    (repo_root / ".av" / "refs" / "heads").mkdir(parents=True)

    commit_hash = "abc1234567"
    commit_data = {
        "message": "initial commit",
        "tags": ["v1"],
        "metrics": {"sharpe": 1.5},
        "tree": {
            "weights/model.safetensors": {"hash": "modelhash", "size": 10, "type": "artifact", "layers": []},
            "data/train.csv": {"hash": "datahash", "size": 20, "type": "artifact"},
            "src/train.py": {"hash": "codehash", "size": 5, "type": "code"},
        },
        "parents": [],
    }
    with open(repo_root / ".av" / "commits" / f"{commit_hash}.json", "w") as f:
        json.dump(commit_data, f)

    (repo_root / ".av" / "refs" / "heads" / "main").write_text(commit_hash)
    (repo_root / ".av" / "HEAD").write_text("ref: refs/heads/main")

    index_entries = {
        rel_path: {**entry, "mtime_ns": 0, "staged": False, "pointer": None}
        for rel_path, entry in commit_data["tree"].items()
    }
    with open(repo_root / ".av" / "index", "w") as f:
        json.dump({"entries": index_entries}, f)

    return commit_hash


def test_generate_handoff_creates_snapshot(tmp_path):
    commit_hash = _init_fake_repo(tmp_path)

    avh_path, md_path = generate_handoff(tmp_path, agent_instructions="fine-tune lr=0.001")

    assert avh_path.name == "handoff.avh"
    with open(avh_path) as f:
        avh_data = json.load(f)
    assert avh_data["current_branch"] == "main"
    assert avh_data["current_commit_hash"] == commit_hash
    assert avh_data["agent_instructions"] == "fine-tune lr=0.001"
    assert avh_data["model_paths"][0]["rel_path"] == "weights/model.safetensors"
    assert avh_data["dataset_lineage"][0]["rel_path"] == "data/train.csv"
    assert avh_data["code_files"] == ["src/train.py"]

    assert md_path.exists()
    assert (tmp_path / "Aether-Handoff" / "Handoff-Hub.md").exists()
    assert (tmp_path / "Aether-Handoff" / "latest.avh").exists()


def test_generate_handoff_update_preserves_instructions(tmp_path):
    _init_fake_repo(tmp_path)

    generate_handoff(tmp_path, agent_instructions="original instructions")
    avh_path, _ = generate_handoff(tmp_path, update=True)

    with open(avh_path) as f:
        avh_data = json.load(f)
    assert avh_data["agent_instructions"] == "original instructions"


def test_generate_handoff_without_update_raises_if_exists(tmp_path):
    _init_fake_repo(tmp_path)
    generate_handoff(tmp_path)

    with pytest.raises(Exception):
        generate_handoff(tmp_path)
