"""Tests for `av stash` (push/list/pop/apply/drop) and the helpers it shares with
`add()`/`checkout()` (stage_one_file/materialize_file/remove_file_and_pointer/resolve_head_tree).
"""
import json
import struct

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli
from python.av_cli.index import Index


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def _make_safetensors(tensors: dict) -> bytes:
    """Minimal valid safetensors blob — see tests/test_cli.py's helper of the same name."""
    header = {}
    offset = 0
    blobs = []
    for name, data in tensors.items():
        header[name] = {"dtype": "U8", "shape": [len(data)], "data_offsets": [offset, offset + len(data)]}
        offset += len(data)
        blobs.append(data)
    header_bytes = json.dumps(header).encode("utf-8")
    return struct.pack("<Q", len(header_bytes)) + header_bytes + b"".join(blobs)


def test_stash_with_nothing_dirty(repo):
    result = invoke("stash")
    assert result.exit_code == 0
    assert "no local changes to stash" in result.output.lower()


def test_stash_push_reverts_staged_new_file(repo):
    (repo / "newfile.py").write_text("brand new")
    invoke("add", "newfile.py")

    result = invoke("stash")
    assert result.exit_code == 0
    assert "saved working directory state" in result.output.lower()

    status = invoke("status")
    assert "nothing to commit" in status.output.lower()
    assert not (repo / "newfile.py").exists()

    listing = invoke("stash", "list")
    assert "stash@{0}" in listing.output


def test_stash_push_reverts_modified_tracked_file(repo):
    (repo / "train.py").write_text("v1")
    invoke("add", "train.py")
    invoke("commit", "-m", "first")

    (repo / "train.py").write_text("v2 dirty")  # modified, never re-added

    result = invoke("stash")
    assert result.exit_code == 0

    status = invoke("status")
    assert "nothing to commit" in status.output.lower()
    assert (repo / "train.py").read_text() == "v1"  # reverted to HEAD


def test_stash_pop_restores_staged_and_modified_state_correctly(repo):
    (repo / "train.py").write_text("v1")
    invoke("add", "train.py")
    invoke("commit", "-m", "first")

    (repo / "train.py").write_text("v2 modified")  # modified, unstaged
    (repo / "newfile.py").write_text("new content")
    invoke("add", "newfile.py")  # staged

    invoke("stash", "-m", "wip")
    result = invoke("stash", "pop")
    assert result.exit_code == 0
    assert "popped" in result.output.lower()

    assert (repo / "train.py").read_text() == "v2 modified"
    assert (repo / "newfile.py").read_text() == "new content"

    idx = Index(repo)
    assert idx.get_entry("train.py")["staged"] is False
    assert idx.get_entry("newfile.py")["staged"] is True

    status = invoke("status")
    assert "Changes not staged for commit" in status.output
    assert "train.py" in status.output
    assert "Changes to be committed" in status.output
    assert "newfile.py" in status.output

    # Stash is consumed by pop.
    listing = invoke("stash", "list")
    assert "no stashes" in listing.output.lower()


def test_stash_apply_keeps_the_stash_record(repo):
    (repo / "newfile.py").write_text("data")
    invoke("add", "newfile.py")
    invoke("stash")

    result = invoke("stash", "apply")
    assert result.exit_code == 0
    assert "applied" in result.output.lower()
    assert (repo / "newfile.py").read_text() == "data"

    listing = invoke("stash", "list")
    assert "stash@{0}" in listing.output  # still there, unlike pop


def test_stash_drop_removes_without_applying(repo):
    (repo / "newfile.py").write_text("data")
    invoke("add", "newfile.py")
    invoke("stash")

    result = invoke("stash", "drop")
    assert result.exit_code == 0
    assert "dropped" in result.output.lower()
    assert not (repo / "newfile.py").exists()  # never restored

    listing = invoke("stash", "list")
    assert "no stashes" in listing.output.lower()


def test_stash_list_orders_newest_first(repo):
    (repo / "a.py").write_text("a")
    invoke("add", "a.py")
    invoke("stash", "-m", "first")

    (repo / "b.py").write_text("b")
    invoke("add", "b.py")
    invoke("stash", "-m", "second")

    listing = invoke("stash", "list")
    lines = [l for l in listing.output.splitlines() if l.strip()]
    assert "second" in lines[0]
    assert "first" in lines[1]


def test_stash_pop_with_no_stashes(repo):
    result = invoke("stash", "pop")
    assert result.exit_code == 0
    assert "no stashes" in result.output.lower()


def test_stash_skips_deleted_files_with_a_warning(repo):
    (repo / "train.py").write_text("v1")
    invoke("add", "train.py")
    invoke("commit", "-m", "first")
    (repo / "train.py").unlink()

    result = invoke("stash")
    assert result.exit_code == 0
    assert "skipping" in result.output.lower()
    assert "no local changes to stash" in result.output.lower()  # nothing else was dirty


def test_stash_push_pop_roundtrip_preserves_safetensors_layers(repo):
    pytest.importorskip("aether_core")
    invoke("config", "1")  # 1 MB LFS threshold

    blob_v1 = _make_safetensors({"layer1": b"A" * (600 * 1024), "layer2": b"B" * (600 * 1024)})
    (repo / "model.safetensors").write_bytes(blob_v1)
    invoke("add", "model.safetensors")
    invoke("commit", "-m", "v1")

    blob_v2 = _make_safetensors({"layer1": b"A" * (600 * 1024), "layer2": b"C" * (600 * 1024)})
    (repo / "model.safetensors").write_bytes(blob_v2)
    invoke("add", "model.safetensors")  # staged, layer-split

    result = invoke("stash")
    assert result.exit_code == 0
    assert (repo / "model.safetensors").read_bytes() == blob_v1  # reverted to HEAD's version

    pop_result = invoke("stash", "pop")
    assert pop_result.exit_code == 0, pop_result.output
    assert (repo / "model.safetensors").read_bytes() == blob_v2  # dirty version restored intact

    idx = Index(repo)
    entry = idx.get_entry("model.safetensors")
    assert entry["staged"] is True
    assert entry["layers"], "expected layer-splitting to have survived the stash round-trip"
