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


def seed_run() -> None:
    """v1.2.5: a real run with 3 linked commits + metrics, for webui/e2e/runs.spec.ts —
    a distinctive name ("e2e-runs-spec-run") rather than a fixed id, since the run id
    is server-generated; the spec resolves it by querying GET /api/runs itself."""
    repo_root = Path(tempfile.mkdtemp(prefix="av-e2e-run-seed-"))
    os.chdir(repo_root)
    runner = CliRunner()

    def run(*args):
        result = runner.invoke(cli, list(args))
        if result.exit_code != 0:
            print(result.output, file=sys.stderr)
            raise SystemExit(f"av {' '.join(args)} failed: {result.exit_code}")
        return result

    run("init")
    run("config", "--remote-url", "http://localhost:8000")
    run("run", "start", "e2e-runs-spec-run")

    for i, loss in enumerate((0.9, 0.5, 0.2), start=1):
        (repo_root / "model.pt").write_bytes(f"weights-v{i}".encode())
        run("add", "model.pt")
        run("commit", "-m", f"e2e run step {i}", "--metric", f"loss={loss}")

    run("run", "finish")
    print(f"Seeded run 'e2e-runs-spec-run' with 3 commits, from {repo_root}")


def seed_rsi() -> None:
    """v1.3.1 (RSI R6, WP-43): a real improver lineage, one PENDING self-edit, a passing
    canary result, and a metric-jump anomaly, for webui/e2e/improver.spec.ts — the
    ImproverPanel/CanaryPanel/RegressionPanel tabs' live-data proof. Distinctive names
    throughout so the spec can pick these rows out of a shared dev registry's history."""
    repo_root = Path(tempfile.mkdtemp(prefix="av-e2e-rsi-seed-"))
    os.chdir(repo_root)
    runner = CliRunner()

    def run(*args):
        result = runner.invoke(cli, list(args))
        if result.exit_code != 0:
            print(result.output, file=sys.stderr)
            raise SystemExit(f"av {' '.join(args)} failed: {result.exit_code}")
        return result

    def run_json(*args):
        result = run("--output", "json", *args)
        return json.loads(result.output)["data"]

    run("init")
    run("config", "--remote-url", "http://localhost:8000")
    (repo_root / "train.py").write_text("print('e2e-rsi-seed')")
    run("add", "train.py")
    run("commit", "-m", "e2e-rsi-seed baseline", "--metric", "val_loss=0.5")

    base = run_json("improver", "init")
    diff_file = repo_root / "change.diff"
    diff_file.write_text("--- a\n+++ b\n-x\n+y")
    cs_applied = run_json("improver", "propose", "--diff", str(diff_file),
                          "--rationale", "e2e-rsi-seed applied edit", "--risk", "low")
    run("improver", "review", cs_applied["id"], "--approve")
    applied = run_json("improver", "apply", cs_applied["id"])

    # A SECOND proposal, deliberately left unresolved — this is what ImproverPanel's
    # "Pending Self-Edits" section renders.
    cs_pending = run_json("improver", "propose", "--diff", str(diff_file),
                          "--rationale", "e2e-rsi-seed pending edit", "--risk", "medium")

    suite = repo_root / "canary-e2e-seed.json"
    suite.write_text(json.dumps({"checks": [
        {"name": "loss_ok", "metric": "val_loss", "op": "<=", "threshold": 0.6}
    ]}))
    run("canary", "register", "e2e-seed-canary", str(suite))
    run("canary", "run", "e2e-seed-canary", "--improver", applied["new_improver_id"])

    # A metric-jump anomaly the RegressionPanel's feed renders.
    (repo_root / "train.py").write_text("print('e2e-rsi-seed v2')")
    run("add", "train.py")
    run("commit", "-m", "e2e-rsi-seed metric jump", "--metric", "val_loss=50.0")

    print(f"Seeded RSI data: improver {base['id']} -> {applied['new_improver_id']}, "
          f"pending change set {cs_pending['id']}, from {repo_root}")


if __name__ == "__main__":
    main()
    seed_run()
    seed_rsi()
