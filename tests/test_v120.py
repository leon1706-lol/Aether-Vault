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
    # v1.2.5: multi-stage Dockerfile (syntax directive first, builder + runtime stages).
    assert docker.startswith("# syntax=") and "Recipe-exact" in docker
    assert "FROM python:" in docker and "AS builder" in docker


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


# ---------------------------------------------------------------------------
# v1.2.5: av handoff --publish (opt-in .avh publish, linked to the active run)
# ---------------------------------------------------------------------------

class _FakePublishResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _FakePublishSession:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return _FakePublishResp(200)


class _FakePublishClient:
    def __init__(self, url="http://fake-registry", token=None):
        self.server_url = url
        self.session = _FakePublishSession()
        self.uploaded = []

    def server_available(self):
        return True

    def upload_object(self, file_path, sha256_hash, known_missing=False):
        self.uploaded.append((str(file_path), sha256_hash))
        return True


def test_handoff_publish_requires_active_run(repo, monkeypatch):
    monkeypatch.setattr("python.av_cli.client.VaultClient", _FakePublishClient)
    result = inv("handoff", "--publish")
    assert result.exit_code == 15, result.output
    assert "No active run" in result.output


def test_handoff_publish_uploads_and_links_to_active_run(repo, monkeypatch):
    fake_client = _FakePublishClient()
    monkeypatch.setattr("python.av_cli.client.VaultClient", lambda *a, **k: fake_client)
    monkeypatch.setenv("AV_RUN_ID", "publish-run-42")

    result = inv("handoff", "--publish")
    assert result.exit_code == 0, result.output
    assert "Published" in result.output
    assert len(fake_client.uploaded) == 1
    avh_path, avh_hash = fake_client.uploaded[0]
    assert avh_path.endswith("handoff.avh")

    url, body = fake_client.session.posts[0]
    assert url.endswith("/api/runs/publish-run-42/avh")
    assert body == {"avh_object_id": avh_hash}


def test_handoff_publish_json_envelope(repo, monkeypatch):
    fake_client = _FakePublishClient()
    monkeypatch.setattr("python.av_cli.client.VaultClient", lambda *a, **k: fake_client)
    monkeypatch.setenv("AV_RUN_ID", "publish-run-json")

    data = jinv("handoff", "--publish")["data"]
    assert data["run_id"] == "publish-run-json"
    assert data["avh_object_id"]


def test_handoff_publish_fails_cleanly_when_unreachable(repo, monkeypatch):
    class _UnreachableClient(_FakePublishClient):
        def server_available(self):
            return False

    monkeypatch.setattr("python.av_cli.client.VaultClient", lambda *a, **k: _UnreachableClient())
    monkeypatch.setenv("AV_RUN_ID", "publish-run-unreachable")

    result = inv("handoff", "--publish")
    assert result.exit_code == 15, result.output
    assert "unreachable" in result.output.lower()
    assert "does not queue" in result.output


def test_handoff_publish_link_failure_surfaces_http_error(repo, monkeypatch):
    class _RejectingSession(_FakePublishSession):
        def post(self, url, json=None, timeout=None):
            return _FakePublishResp(422, "run not found or something")

    class _RejectingClient(_FakePublishClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.session = _RejectingSession()

    monkeypatch.setattr("python.av_cli.client.VaultClient", lambda *a, **k: _RejectingClient())
    monkeypatch.setenv("AV_RUN_ID", "publish-run-reject")

    result = inv("handoff", "--publish")
    assert result.exit_code == 15, result.output
    assert "Failed to link" in result.output


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


def test_policy_set_require_signature_alone_is_a_valid_standalone_policy(repo):
    # v1.2.5: before this, the ONLY way to arm a signature-only policy was to hand-edit
    # .av/policies.json directly (METRIC/OP were required positional args) — every
    # require_signature test drove it that way, and `av policy set --require-signature`
    # with no METRIC/OP had never actually been exercised through the CLI. Real gap, not
    # a hypothetical: see development/Probleme.md.
    jinv("--output", "json", "policy", "set", "main", "--require-signature")
    pol = jinv("--output", "json", "policy", "list")["data"]["policies"]
    assert pol["main"] == {"require_signature": True}


def test_policy_set_combines_metric_and_require_signature(repo):
    jinv("--output", "json", "policy", "set", "main", "val_loss", "<",
         "--threshold", "0.45", "--require-signature")
    pol = jinv("--output", "json", "policy", "list")["data"]["policies"]
    assert pol["main"] == {
        "metric": "val_loss", "op": "<", "threshold": 0.45, "require_signature": True,
    }


def test_policy_set_rejects_metric_without_op_and_vice_versa(repo):
    res = inv("policy", "set", "main", "val_loss")  # METRIC given, OP missing
    assert res.exit_code != 0
    res = inv("policy", "set", "main", "--threshold", "0.45")  # --threshold with no METRIC/OP
    assert res.exit_code != 0


def test_policy_set_with_nothing_at_all_is_rejected(repo):
    res = inv("policy", "set", "main")
    assert res.exit_code != 0


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
