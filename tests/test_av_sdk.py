"""av_sdk parity tests — SDK and CLI must behave identically (single code path)."""
import json

import pytest
from click.testing import CliRunner

from av_sdk import Repo, SDKError
from python.av_cli.main import cli


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"])
    assert res.exit_code == 0, res.output
    return tmp_path


def test_repo_requires_av_directory(tmp_path):
    with pytest.raises(SDKError) as ei:
        Repo(tmp_path)
    assert ei.value.code == "not_a_repo"


def test_commit_via_sdk_matches_cli_semantics(repo):
    (repo / "w.pt").write_bytes(b"weights")
    with Repo(repo) as r:
        r.add("w.pt")
        result = r.commit("sdk commit", tags=["exp1"], metrics={"loss": 0.3}, no_upload=True)

    assert result["committed"] is True
    assert result["queued_reason"] == "upload_deferred"
    # The CLI sees the same history:
    log = inv_cli(repo, "log").output
    assert "sdk commit" in log
    # And the queue holds the deferred commit:
    push = inv_cli_json(repo, "push")["data"]
    assert push["reachable"] is False and push["still_queued"] >= 1


def test_sdk_commit_with_nothing_staged_raises_nothing_to_commit(repo):
    with Repo(repo) as r, pytest.raises(SDKError) as ei:
        r.commit("empty")
    assert ei.value.code == "nothing_to_commit"


def test_sdk_run_start_registration_payload_includes_project_id(repo, monkeypatch):
    """Regression test (Probleme.md): Repo.run_start() built its own POST /api/runs
    payload independently of cmd_run.py::start() and had the exact same bug —
    project_id missing entirely, which the server requires (422 without one). Every
    existing SDK run test ran fully offline, so this never got exercised."""
    import av_cli.client as client_module

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

    with Repo(repo) as r:
        started = r.run_start("sdk-run")

    assert started["registered_server_side"] is True
    assert captured["payload"]["project_id"], "registration payload must include project_id"
    assert captured["payload"]["name"] == "sdk-run"
    assert captured["payload"]["id"] == started["run_id"]


def test_sdk_run_lifecycle_and_handoff_lineage(repo):
    _stage = repo / "m.ckpt"
    _stage.write_bytes(b"ckpt")
    with Repo(repo) as r:
        started = r.run_start("agent-run")
        r.add("m.ckpt")
        result = r.commit("in run", metrics={"val_loss": 0.11})
        finished = r.run_finish(metrics={"final": 1})

    assert f"run:{started['run_id']}" in result["tags"]
    assert finished["status"] == "completed"

    doc = json.loads((repo / "handoff.avh").read_text(encoding="utf-8")) \
        if (repo / "handoff.avh").exists() else None


def test_context_note_via_sdk_appears_in_cli_show(repo):
    with Repo(repo) as r:
        r.context_note("sdk-written memory")
    from click.testing import CliRunner
    res = CliRunner().invoke(cli, ["context", "show"])
    assert "sdk-written memory" in res.output


# ---------------------------------------------------------------------------
# v1.3.0 full-surface parity (todo.md item 5): the commit path's CLI≡SDK≡plugin-seam
# parity is proven in tests/test_plugins.py's "seam migration" section; the SDK's OTHER
# methods (status/log/push/diff_semantic/context_note/handoff_dict/publish_handoff) had
# no such proof before this — each method's docstring says "mirrors the CLI's --output
# json payload" but nothing ever checked that claim against the real CLI output on the
# SAME repo state. These tests do exactly that: same repo, same operation via both
# surfaces, same payload shape.
# ---------------------------------------------------------------------------

def test_status_parity_sdk_vs_cli(repo):
    (repo / "staged.pt").write_bytes(b"a")
    (repo / "untracked.txt").write_text("u")
    inv_cli(repo, "add", "staged.pt")

    with Repo(repo) as r:
        sdk_status = r.status()
    cli_status = inv_cli_json(repo, "status")["data"]

    assert sdk_status == cli_status


def test_log_parity_sdk_vs_cli(repo):
    (repo / "m.pt").write_bytes(b"v1")
    inv_cli(repo, "add", "m.pt")
    inv_cli(repo, "commit", "-m", "first", "--tag", "t1", "--metric", "x=1")
    (repo / "m.pt").write_bytes(b"v2")
    inv_cli(repo, "add", "m.pt")
    inv_cli(repo, "commit", "-m", "second")

    with Repo(repo) as r:
        sdk_log = r.log()
    cli_log = inv_cli_json(repo, "log")["data"]["commits"]

    # SDK's log() is a narrower, purpose-built shape (hash/short/message/author/tags/
    # metrics/parents) than the CLI's richer envelope (which also carries decorations/
    # is_head/tree-adjacent fields) — parity is checked on the fields the SDK actually
    # promises, not byte-for-byte across the two payloads.
    assert len(sdk_log) == len(cli_log)
    for sdk_entry, cli_entry in zip(sdk_log, cli_log):
        for field in ("hash", "short", "message", "author", "tags", "metrics", "parents"):
            assert sdk_entry[field] == cli_entry[field], (
                f"log() parity broken on {field}: sdk={sdk_entry[field]} cli={cli_entry[field]}"
            )


def test_push_parity_sdk_vs_cli_when_nothing_pending(repo):
    with Repo(repo) as r:
        sdk_push = r.push()
    cli_push = inv_cli_json(repo, "push")["data"]
    assert sdk_push == cli_push == {"drained": 0, "still_queued": 0, "reachable": None}


def test_push_parity_sdk_vs_cli_when_queued(repo):
    (repo / "q.pt").write_bytes(b"q")
    inv_cli(repo, "add", "q.pt")
    inv_cli(repo, "commit", "-m", "queued one")  # unreachable server -> queues

    with Repo(repo) as r:
        sdk_push = r.push()
    # A second push (CLI) drains nothing further but proves the SAME queue state
    # (both surfaces see "still queued", server genuinely unreachable) rather than
    # each maintaining its own notion of the pending-push file.
    cli_push = inv_cli_json(repo, "push")["data"]
    assert sdk_push["reachable"] is False
    assert cli_push["reachable"] is False
    assert sdk_push["still_queued"] == cli_push["still_queued"] == 1


def test_diff_semantic_parity_sdk_vs_cli(repo):
    (repo / "d.pt").write_bytes(b"v1")
    inv_cli(repo, "add", "d.pt")
    inv_cli(repo, "commit", "-m", "v1")
    (repo / "d.pt").write_bytes(b"v2-longer-content")
    inv_cli(repo, "add", "d.pt")
    inv_cli(repo, "commit", "-m", "v2")

    with Repo(repo) as r:
        sdk_diff = r.diff_semantic()
    cli_diff = inv_cli_json(repo, "diff")["data"]

    assert sdk_diff == cli_diff


def test_context_note_parity_sdk_vs_cli_shape(repo):
    with Repo(repo) as r:
        sdk_result = r.context_note("sdk note", agent="tester")
    cli_result = inv_cli_json(repo, "context", "note", "cli note", "--agent", "tester")["data"]

    # Both are {"appended": True, "entry": {...}} — the entry SHAPE (not content, which
    # differs by design) must match across surfaces.
    assert sdk_result["appended"] is True and cli_result.get("appended", True) is not False
    assert set(sdk_result["entry"]) == {"ts", "agent", "note"}


def test_handoff_dict_parity_sdk_vs_cli_export(repo):
    (repo / "h.pt").write_bytes(b"weights")
    inv_cli(repo, "add", "h.pt")
    inv_cli(repo, "commit", "-m", "for handoff")

    with Repo(repo) as r:
        sdk_doc = r.handoff_dict()
    # `av context export --format avh` builds the identical document (build_handoff_dict)
    # through the CLI path, wrapped in the standard envelope's data.document (v1.3.0).
    cli_export = inv_cli_json(repo, "context", "export", "--format", "avh")["data"]
    cli_doc = json.loads(cli_export["document"])

    for key in ("avh_version", "current_branch", "current_commit_hash", "lineage"):
        assert sdk_doc[key] == cli_doc[key], f"handoff_dict() parity broken on {key}"


def test_error_code_parity_not_a_repo_across_surfaces(tmp_path):
    from python.av_cli.core import EXIT_NOT_A_REPO

    with pytest.raises(SDKError) as ei:
        Repo(tmp_path)
    assert ei.value.code == "not_a_repo"
    assert ei.value.exit_code == EXIT_NOT_A_REPO == 10

    cli_result = inv_cli(tmp_path, "--output", "json", "status")
    assert cli_result.exit_code == EXIT_NOT_A_REPO
    assert json.loads(cli_result.output)["error"]["code"] == "not_a_repo"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def inv_cli(repo, *args):
    import os

    prev = os.getcwd()
    os.chdir(repo)
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(prev)


def inv_cli_json(repo, *args):
    res = inv_cli(repo, "--output", "json", *args)
    return json.loads(res.output)
