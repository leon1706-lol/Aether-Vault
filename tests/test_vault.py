import os
import json
import pytest
from pathlib import Path

from python.av_cli.pointer import create_pointer, parse_pointer, is_pointer_file, get_pointer_path
from python.av_cli.index import Index

def test_create_and_parse_pointer(tmp_path):
    p = tmp_path / "model.pt"
    p.write_text("dummy content")
    
    ptr_content = create_pointer(p, "fakehash123", 1024)
    assert "fakehash123" in ptr_content
    assert "1024" in ptr_content
    
    parsed = parse_pointer(ptr_content)
    assert parsed["hash"] == "fakehash123"
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
