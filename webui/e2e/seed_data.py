#!/usr/bin/env python
"""Seed two real commits (with a layer-split .safetensors checkpoint) into the live
aether-vault-server stack, via the actual `av` CLI — for the Playwright E2E tests to exercise.

Run with the repo's main Python install (the one `av` is registered against), against an
already-running `docker compose up -d db redis aether-vault-server`:

    python webui/e2e/seed_data.py
"""
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

from click.testing import CliRunner

from av_cli.main import cli


def _make_safetensors(tensors: dict) -> bytes:
    """Mirrors tests/test_core.py's helper: 8-byte LE header length + JSON header + tensor data."""
    header = {}
    offset = 0
    blobs = []
    for name, data in tensors.items():
        header[name] = {
            "dtype": "U8",
            "shape": [len(data)],
            "data_offsets": [offset, offset + len(data)],
        }
        offset += len(data)
        blobs.append(data)
    header_bytes = json.dumps(header).encode("utf-8")
    return struct.pack("<Q", len(header_bytes)) + header_bytes + b"".join(blobs)


def main() -> None:
    repo_root = Path(tempfile.mkdtemp(prefix="av-e2e-seed-"))
    os.chdir(repo_root)
    runner = CliRunner()

    def run(*args):
        result = runner.invoke(cli, list(args))
        if result.exit_code != 0:
            print(result.output, file=sys.stderr)
            raise SystemExit(f"av {' '.join(args)} failed: {result.exit_code}")
        return result

    run("init")
    run("config", "1")  # 1 MB LFS threshold so the checkpoint goes through the pointer path
    run("config", "--remote-url", "http://localhost:8000")

    # v1: two tensors, "layer1" and "layer2".
    # A distinctive filename (not just "model.safetensors") so the Playwright spec can reliably
    # pick out these two specific rows in the checkpoint list even if the shared dev registry
    # already has other, unrelated checkpoints pushed in past manual-testing sessions.
    ckpt = repo_root / "weights" / "e2e-weight-diff.safetensors"
    ckpt.parent.mkdir(parents=True)
    ckpt.write_bytes(_make_safetensors({
        "layer1.weight": b"A" * (1024 * 1024 + 16),  # exceed the 1 MB threshold
        "layer2.weight": b"B" * 1024,
    }))
    run("add", "weights/e2e-weight-diff.safetensors")
    run("commit", "-m", "v1 checkpoint", "--tag", "e2e-seed")

    # v2: layer2 changes, layer1 stays the same — gives the Weight Diff view a real
    # changed/unchanged mix to render.
    ckpt.write_bytes(_make_safetensors({
        "layer1.weight": b"A" * (1024 * 1024 + 16),
        "layer2.weight": b"C" * 1024,
    }))
    run("add", "weights/e2e-weight-diff.safetensors")
    run("commit", "-m", "v2 checkpoint", "--tag", "e2e-seed")

    print(f"Seeded 2 commits with a layer-split checkpoint, from {repo_root}")


if __name__ == "__main__":
    main()
