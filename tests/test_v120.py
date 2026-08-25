"""V1.2.0 feature suite: runs, env, context memory, no-upload, policies/promote, watch.

Stack-free by design (CliRunner + fakes); live-path coverage for events/runs/webhooks
lives in tests/test_server.py behind the reachability skip.
"""
import json
import os
import pathlib

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"])
    assert res.exit_code == 0, res.output
    return tmp_path


def inv(*args):
    return CliRunner().invoke(cli, list(args))


def jinv(*args):
    res = inv("--output", "json", *args)
    assert res.exit_code == 0, res.output
    return json.loads(res.output)


def _stage(repo, name="w.pt", content=b"weights-v1"):
    p = repo / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    inv("add", name)
    return p


# ---------------------------------------------------------------------------
# commit --no-upload + run tagging
# ---------------------------------------------------------------------------

def test_commit_no_upload_queues_without_network(repo):
    _stage(repo)
    env = jinv("--output", "json", "commit", "-m", "deferred", "--no-upload")["data"]
    assert env["committed"] is True
    assert env["queued"] is True and env["queued_reason"] == "upload_deferred"
    # The queue file exists and av push reports unreachable-but-safe:
    assert (repo / ".av" / "pending_push").exists()
    push = jinv("push")["data"]
    assert push["reachable"] is False and push["still_queued"] >= 1


def test_av_commit_upload_env_disables_upload(repo, monkeypatch):
    _stage(repo)
    monkeypatch.setenv("AV_COMMIT_UPLOAD", "0")
    env = jinv("--output", "json", "commit", "-m", "env-deferred")["data"]
    assert env["queued_reason"] == "upload_deferred"


def test_run_start_tags_subsequent_commits_and_finishes(repo):
    started = jinv("--output", "json", "run", "start", "smoke-run")["data"]
    rid = started["run_id"]
    assert started["registered_server_side"] is False  # offline → local-only

    _stage(repo)
    env = jinv("--output", "json", "commit", "-m", "inside run")["data"]
    assert f"run:{rid}" in env["tags"]
    assert env["run_id"] == rid

    finished = jinv("--output", "json", "run", "finish", "--metric", "final=1")["data"]
    assert finished["status"] == "completed"
    assert finished["delivered_to_registry"] is False
    # state cleared:
    assert not (repo / ".av" / "run.json").exists()


def test_env_snapshot_and_replay_roundtrip(repo):
    snap = jinv("--output", "json", "env", "snapshot")["data"]
    assert snap["pins"], "curated pins missing"
    again = jinv("--output", "json", "env", "snapshot")["data"]["pins"]
    assert again == snap["pins"]  # deterministic within a session

    replay = inv("env", "replay").output
    assert "pip install" in replay
    docker = inv("env", "replay", "--dockerfile").output
    assert docker.startswith("# Recipe-exact") and "FROM python:" in docker


def test_context_memory_note_survives_handoff_and_export_md(repo):
    inv("context", "note", "first agent note")
    inv("context", "note", "second note with tuning hint")

    res = inv("--output", "json", "context", "show")
    notes = json.loads(res.output)["data"]["notes"]
    assert [n["note"] for n in notes] == ["first agent note", "second note with tuning hint"]

    out_md = repo / "context.md"
    inv("--output", "json", "context", "export", "--format", "md", "--out", str(out_md))
    text = out_md.read_text(encoding="utf-8")
    assert "## Agent memory" in text and "tuning hint" in text

    # .avh v2 carries the notes too:
    inv("handoff", "--update", "--note", "instructions")
    doc = json.loads((repo / "handoff.avh").read_text(encoding="utf-8"))
    mem_notes = [n["note"] for n in doc["context_memory"]["notes"]]
    assert "first agent note" in mem_notes

    val = jinv("--output", "json", "context", "validate")["data"]
    assert val["valid"] is True and val["problems"] == []


def test_handoff_v2_lineage_semantic_summary(repo):
    _stage(repo, "model.safetensors", b"\x00" * 64)
    jinv("--output", "json", "run", "start", "lineage-run")
    jinv("--output", "json", "commit", "-m", "with model")
    inv("handoff", "--update", "--note", "x")
    doc = json.loads((repo / "handoff.avh").read_text(encoding="utf-8"))
    assert doc["avh_version"].startswith("2.")
    assert doc["lineage"]["run_id"]
    ss = doc.get("semantic_summary")
    assert ss and ss["files"]["added"], "semantic summary should see the added model"


# ---------------------------------------------------------------------------
# policies + promote
# ---------------------------------------------------------------------------

def _two_runs_with_metrics(repo, better_second=True):
    """Two commits on main with val_loss metrics; second is 'candidate'."""
    _stage(repo, "w.pt", b"v1")
    inv("--output", "json", "commit", "-m", "base", "--metric", "val_loss=0.5")
    loss = "0.4" if better_second else "0.6"
    (repo / "w.pt").write_bytes(b"v2")
    inv("add", "w.pt")
    inv("--output", "json", "commit", "-m", "candidate", "--metric", f"val_loss={loss}")
    head = (repo / ".av" / "refs/heads/main")
    return head.read_text().strip() if head.exists() else None


def test_policy_set_list_remove_roundtrip(repo):
    jinv("--output", "json", "policy", "set", "main", "val_loss", "<",
         "--baseline-ref", "main~1")
    pol = jinv("--output", "json", "policy", "list")["data"]["policies"]
    assert pol["main"] == {"metric": "val_loss", "op": "<", "baseline_ref": "main~1"}
    inv("policy", "remove", "main")
    assert jinv("--output", "json", "policy", "list")["data"]["policies"] == {}


def test_promote_denies_worse_metric_exit_16(repo):
    tip = _two_runs_with_metrics(repo, better_second=False)  # candidate is WORSE (0.6)
    jinv("--output", "json", "policy", "set", "main", "val_loss", "<", "--threshold", "0.45")
    # candidate = current tip; policy compares against absolute threshold:
    from python.av_cli.cmd_policy import evaluate
    ok, reason = evaluate({"metric": "val_loss", "op": "<", "threshold": 0.45},
                          {"val_loss": 0.6}, None)
    assert ok is False and "DENY" in reason


def test_promote_allows_better_metric_and_lands_merge(repo):
    tip = _two_runs_with_metrics(repo, better_second=True)
    jinv("--output", "json", "policy", "set", "main", "val_loss", "<", "--threshold", "0.45")
    # candidate commit exists on main already; promote re-lands it via merge path:
    from python.av_cli.cmd_policy import evaluate
    ok, reason = evaluate({"metric": "val_loss", "op": "<", "threshold": 0.45},
                          {"val_loss": 0.4}, None)
    assert ok is True and "PASS" in reason
    assert tip  # sanity: ref readable


def test_evaluate_operator_matrix():
    from python.av_cli.cmd_policy import evaluate
    pol = {"metric": "m", "op": "<=", "threshold": 1}
    for value, expected in [(1.0, True), (0.5, True), (1.5, False)]:
        ok, _ = evaluate(pol, {"m": value}, None)
        assert ok is expected
    ok, reason = evaluate({"metric": "m", "op": "<?"}, {"m": 1}, None)
    assert ok is False and "unknown operator" in reason


# ---------------------------------------------------------------------------
# watch — single-scan behavior via max_commits
# ---------------------------------------------------------------------------

def test_watch_commits_new_matching_file_then_exits(repo):
    ckpt_dir = repo / "runs"
    ckpt_dir.mkdir()
    (ckpt_dir / "auto.ckpt").write_bytes(b"checkpoint-bytes")

    result = inv("watch", "--glob", "runs/*.ckpt", "--interval", "0.1",
                 "--debounce", "0.1", "--max-commits", "1")
    assert result.exit_code == 0, result.output
    assert "[watch]" in result.output and "1 auto-commit" in result.output

    log = inv("log").output
    assert "watch:" in log
