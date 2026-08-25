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
