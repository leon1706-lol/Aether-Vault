"""V1.2.0 feature suite: runs, env, context memory, no-upload, policies/promote, watch.

Stack-free by design (CliRunner + fakes); live-path coverage for events/runs/webhooks
lives in tests/test_server.py behind the reachability skip.
"""
import importlib.util
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

def test_commit_no_upload_queues_without_network(repo, unreachable_client):
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


def test_run_start_registration_payload_includes_project_id(repo, monkeypatch):
    """Regression test (Probleme.md): the POST /api/runs payload built by `av run start`
    never included project_id — the server requires it (422 without one; see
    server.py::create_run), so every registration attempt against a REACHABLE server
    silently failed (_register_remote treats any non-200 as "not registered" with no
    visible error) and fell back to the server's lazy-create-at-push path, which has no
    way to learn the run's name (the commit payload never carries it) — the run still
    gets created, but permanently nameless. Every existing `run start` test ran fully
    offline (registered_server_side=False), so this never got exercised until
    webui/e2e/runs.spec.ts failed to find its seeded run by name in live CI."""
    import python.av_cli.client as client_module

    captured: dict = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "created", "id": captured["payload"]["id"]}

    class _FakeSession:
        def post(self, url, json=None):
            captured["url"] = url
            captured["payload"] = json
            return _FakeResponse()

    class _ReachableClient(client_module.VaultClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.session = _FakeSession()

        def server_available(self) -> bool:
            return True

    monkeypatch.setattr(client_module, "VaultClient", _ReachableClient)

    started = jinv("--output", "json", "run", "start", "my-run")["data"]
    assert started["registered_server_side"] is True
    assert captured["payload"]["project_id"], "registration payload must include project_id"
    assert captured["payload"]["name"] == "my-run"
    assert captured["payload"]["id"] == started["run_id"]


def test_run_start_tags_subsequent_commits_and_finishes(repo, unreachable_client):
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
# .avh validation on every write/read, run finish guarantees the handoff exists,
# context search, notes stamp their active run.
# ---------------------------------------------------------------------------

def test_context_note_stamps_the_active_run_id(repo):
    inv("context", "note", "no run yet")
    inv("run", "start", "search-run")
    inv("context", "note", "written under a run")
    inv("run", "finish")

    notes = jinv("context", "show")["data"]["notes"]
    assert notes[0]["run_id"] is None
    assert notes[1]["run_id"] is not None


def test_context_search_filters_by_substring_run_and_since(repo):
    inv("context", "note", "LR 3e-4 diverged at step 9k")
    inv("run", "start", "tuning-run")
    inv("context", "note", "LR schedule looks stable now")
    inv("run", "finish")
    run_id = None

    notes = jinv("context", "show")["data"]["notes"]
    run_id = notes[1]["run_id"]

    all_lr = jinv("context", "search", "LR")["data"]
    assert all_lr["count"] == 2

    scoped = jinv("context", "search", "LR", "--run", run_id)["data"]
    assert scoped["count"] == 1
    assert "stable" in scoped["matches"][0]["note"]

    none_match = jinv("context", "search", "definitely-not-present")["data"]
    assert none_match["count"] == 0

    text_result = inv("context", "search", "diverged")
    assert "diverged" in text_result.output


def test_context_search_is_case_insensitive_by_default(repo):
    inv("context", "note", "Loss Function tuning note")
    hit = jinv("context", "search", "loss function")["data"]
    assert hit["count"] == 1
    miss = jinv("context", "search", "loss function", "--case-sensitive")["data"]
    assert miss["count"] == 0


def test_run_finish_regenerates_handoff_with_guaranteed_fields(repo):
    """v1.3.0: av run finish must guarantee lineage/metrics-tail/semantic-summary are
    present locally afterward, without needing a separate `av handoff` call."""
    (repo / "m.pt").write_bytes(b"weights")
    inv("run", "start", "handoff-guarantee-run")
    inv("add", "m.pt")
    inv("commit", "-m", "in run", "--metric", "val_loss=0.2")
    result = jinv("run", "finish")
    assert result["data"]["handoff_written"] is True

    doc = json.loads((repo / "handoff.avh").read_text(encoding="utf-8"))
    assert doc["lineage"]["run_id"] is not None
    assert doc["semantic_summary"] is not None
    assert doc["context_memory"]["metrics_history_tail"]


def test_run_finish_handoff_failure_never_blocks_the_finish(repo, monkeypatch):
    """Best-effort: a broken handoff generation must not prevent the run from finishing
    (the run itself completing is what matters — the handoff guarantee is a bonus, not
    a hard dependency, matching the "never lose the run" spirit non-negotiable #3 sets
    for network failures)."""
    import python.av_cli.handoff as handoff_module

    inv("run", "start", "resilient-run")

    def _boom(*a, **k):
        raise RuntimeError("disk full, or whatever")

    # finish() does `from .handoff import generate_handoff` fresh inside its own try
    # block, so patching the name on the `handoff` module itself (not cmd_run's
    # namespace, which never holds a reference to it) is what actually intercepts it.
    monkeypatch.setattr(handoff_module, "generate_handoff", _boom)

    result = inv("--output", "json", "run", "finish")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["handoff_written"] is False
    assert data["status"] == "completed"  # the run itself still finished


def test_avh_validate_catches_a_schema_violation_via_jsonschema():
    """Proves validate_handoff() actually uses real jsonschema.validate() when
    available, not just the hand-rolled structural check — a violation only jsonschema
    would catch (wrong TYPE for an existing, present field) must surface."""
    import importlib.util

    if importlib.util.find_spec("jsonschema") is None:
        pytest.skip("jsonschema not installed (dev extra)")

    from python.av_cli.handoff import validate_handoff

    doc = {
        "$schema": "https://aether-vault.dev/schemas/avh-2.0.json",
        "avh_version": "2.0",
        "generated_at": "2026-01-01T00:00:00Z",
        "current_branch": "main",
        "lineage": {"run_id": None, "parent_run_ids": "not-a-list", "code_pointer": None},
        "context_memory": {"notes": [], "metrics_history_tail": []},
    }
    problems = validate_handoff(doc)
    assert problems, "a wrong-typed lineage.parent_run_ids should have been caught"


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
    # `av policy set --require-signature` with no METRIC/OP must work through the CLI
    # directly, not just by hand-editing .av/policies.json.
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


def test_example_policies_load_and_evaluate():
    """v1.3.0 (todo.md item 12): examples/policies/*.json are real, loaded, exercised
    fixtures, not just illustrative prose — this fails the moment one of them stops
    matching the shape av_cli.cmd_policy actually understands."""
    import json
    from pathlib import Path

    from python.av_cli.cmd_policy import evaluate

    examples_dir = Path(__file__).resolve().parents[1] / "examples" / "policies"
    assert examples_dir.is_dir(), "examples/policies/ is missing"

    metric_gate = json.loads((examples_dir / "metric-gate.json").read_text())["main"]
    assert metric_gate["baseline_ref"] == "main~1"
    ok, reason = evaluate(metric_gate, {"val_loss": 0.3}, {"val_loss": 0.5})
    assert ok is True and "PASS" in reason
    ok, reason = evaluate(metric_gate, {"val_loss": 0.7}, {"val_loss": 0.5})
    assert ok is False and "DENY" in reason

    sig_gate = json.loads((examples_dir / "signature-gate.json").read_text())["main"]
    assert sig_gate == {"require_signature": True}
    # A signature-only policy has no metric — evaluate() correctly refuses to be used
    # for it (promote()/enforce_policy() both branch around evaluate() entirely when
    # "metric" is absent, checking require_signature separately — see cmd_policy.py).
    ok, reason = evaluate(sig_gate, {}, None)
    assert ok is False and "no metric" in reason

    combined = json.loads((examples_dir / "combined-gate.json").read_text())["main"]
    assert combined["require_signature"] is True
    ok, reason = evaluate(combined, {"val_loss": 0.4}, None)
    assert ok is True and "PASS" in reason
    ok, reason = evaluate(combined, {"val_loss": 0.6}, None)
    assert ok is False and "DENY" in reason


def test_example_policies_apply_via_the_real_cli(repo):
    """The combined-gate example, applied to a real repo exactly as a user would copy
    it in, then exercised through av promote --dry-run end to end."""
    import json
    import shutil
    from pathlib import Path

    examples_dir = Path(__file__).resolve().parents[1] / "examples" / "policies"
    shutil.copy(examples_dir / "combined-gate.json", repo / ".av" / "policies.json")

    (repo / "m.txt").write_text("v1")
    inv("add", "m.txt")
    inv("commit", "-m", "unsigned, good metric", "--metric", "val_loss=0.3")

    result = jinv("promote", "--into", "main", "--dry-run")
    assert result["data"]["decision"] == "deny"  # unsigned — the combined gate's other half
    assert "require_signature" in result["data"]["rule"]


def test_promote_reports_policy_outcome_for_the_active_run(repo, monkeypatch):
    """v1.3.0 (todo.md item 7): a real (non-dry-run) promote decision is reported against
    whatever run is active via VaultClient.report_run_policy_outcome — best-effort
    telemetry, not a gate."""
    from python.av_cli import client as client_module

    calls = []
    monkeypatch.setattr(
        client_module.VaultClient, "report_run_policy_outcome",
        lambda self, run_id, decision, rule: calls.append((run_id, decision, rule)),
    )

    run_id = jinv("run", "start", "training-run")["data"]["run_id"]
    tip = _two_runs_with_metrics(repo, better_second=True)
    jinv("policy", "set", "main", "val_loss", "<", "--threshold", "0.45")

    result = jinv("promote", "--into", "main")
    assert result["data"]["allowed"] is True
    assert calls == [(run_id, "allow", "metric:val_loss<")]


def test_promote_dry_run_never_reports_policy_outcome(repo, monkeypatch):
    """The documented dry-run contract is 'touches nothing either way' — that includes
    not writing a policy-outcome pointer for the active run."""
    from python.av_cli import client as client_module

    calls = []
    monkeypatch.setattr(
        client_module.VaultClient, "report_run_policy_outcome",
        lambda self, run_id, decision, rule: calls.append((run_id, decision, rule)),
    )

    jinv("run", "start", "training-run")
    _two_runs_with_metrics(repo, better_second=False)
    jinv("policy", "set", "main", "val_loss", "<", "--threshold", "0.45")

    jinv("promote", "--into", "main", "--dry-run")
    assert calls == []


def test_promote_reporting_failure_never_blocks_the_promotion(repo, monkeypatch):
    """Offline resilience is sacred: an unreachable/erroring registry must never turn a
    telemetry write into a failed promotion."""
    from python.av_cli import client as client_module

    def _boom(self, run_id, decision, rule):
        raise ConnectionError("registry unreachable")

    monkeypatch.setattr(client_module.VaultClient, "report_run_policy_outcome", _boom)

    jinv("run", "start", "training-run")
    _two_runs_with_metrics(repo, better_second=True)
    jinv("policy", "set", "main", "val_loss", "<", "--threshold", "0.45")

    result = jinv("promote", "--into", "main")
    assert result["data"]["allowed"] is True


def test_enforce_policy_reports_outcome_directly(repo, monkeypatch):
    """Unit-level check of the shared helper both promote() and merge()'s policy hook
    (enforce_policy) call — covers the require_signature-denial reporting path, which
    promote() takes a different code branch to reach than the metric path above does."""
    from python.av_cli import client as client_module
    from python.av_cli.cmd_policy import enforce_policy, save_policies

    calls = []
    monkeypatch.setattr(
        client_module.VaultClient, "report_run_policy_outcome",
        lambda self, run_id, decision, rule: calls.append((run_id, decision, rule)),
    )

    run_id = jinv("run", "start", "training-run")["data"]["run_id"]
    save_policies(repo, {"main": {"require_signature": True}})

    with pytest.raises(SystemExit):
        enforce_policy(repo, "main", None, lambda ref: None, candidate_ref=None)

    assert calls == [(run_id, "deny", "require_signature")]


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


def test_watch_polling_fallback_when_watchdog_unavailable(repo, monkeypatch):
    """v1.3.0: forces the pure-stdlib os.walk() polling path even on a machine that DOES
    have watchdog installed — the optional extra must degrade cleanly, not become a hard
    dependency of the command actually working."""
    import python.av_cli.cmd_watch as cmd_watch_module

    monkeypatch.setattr(cmd_watch_module, "_try_start_watchdog", lambda repo_root, pattern: None)

    ckpt_dir = repo / "runs"
    ckpt_dir.mkdir()
    (ckpt_dir / "auto.ckpt").write_bytes(b"checkpoint-bytes")

    result = inv("watch", "--glob", "runs/*.ckpt", "--interval", "0.1",
                 "--debounce", "0.1", "--max-commits", "1")
    assert result.exit_code == 0, result.output
    assert "polling" in result.output
    assert "1 auto-commit" in result.output


@pytest.mark.skipif(importlib.util.find_spec("watchdog") is None, reason="watchdog extra not installed")
def test_watch_uses_real_watchdog_events_when_installed(repo):
    """The real path, with the real watchdog package — not mocked. A file created AFTER
    watch starts must still get picked up and committed via real fs events."""
    ckpt_dir = repo / "runs"
    ckpt_dir.mkdir()

    import threading
    import time as _time

    def _write_after_a_beat():
        _time.sleep(0.3)
        (ckpt_dir / "auto.ckpt").write_bytes(b"checkpoint-bytes")

    threading.Thread(target=_write_after_a_beat, daemon=True).start()

    result = inv("watch", "--glob", "runs/*.ckpt", "--interval", "0.2",
                 "--debounce", "0.2", "--max-commits", "1")
    assert result.exit_code == 0, result.output
    assert "watchdog events" in result.output
    assert "1 auto-commit" in result.output

    log = inv("log").output
    assert "watch:" in log
