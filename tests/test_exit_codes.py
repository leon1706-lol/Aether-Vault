"""Table-driven proof of the documented exit-code registry (v1.2.5).

Before this file existed, four of the seven codes AGENTS.md/README/architecture.md publish
were aspirational: `not_a_repo`/`auth_failed` exited 1 (ClickException's default), and
`nothing_to_commit`/`merge_conflict` exited 0 — see the V1.2.5 plan's "Three real bugs"
note and development/Probleme.md. This file provokes each code through the real CLI (not
mocks) and pins the exact exit code, in both text and --output json mode, so the registry
can never silently drift from the docs again.
"""

import json

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def invoke_json(*args):
    return CliRunner().invoke(cli, ["--output", "json", *args])


# ---------------------------------------------------------------------------
# 10 — not_a_repo
# ---------------------------------------------------------------------------

def test_not_a_repo_exits_10_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("status")
    assert result.exit_code == 10, result.output
    assert "not an aether-vault repository" in result.output.lower()


def test_not_a_repo_exits_10_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke_json("status")
    assert result.exit_code == 10, result.output
    env = json.loads(result.output)
    assert env["ok"] is False
    assert env["error"]["code"] == "not_a_repo"


# ---------------------------------------------------------------------------
# 11 — nothing_to_commit
# ---------------------------------------------------------------------------

def test_nothing_to_commit_exits_11_text(repo):
    result = invoke("commit", "-m", "empty")
    assert result.exit_code == 11, result.output
    assert "nothing to commit" in result.output.lower()


def test_nothing_to_commit_exits_11_json(repo):
    result = invoke_json("commit", "-m", "empty")
    assert result.exit_code == 11, result.output
    env = json.loads(result.output)
    assert env["ok"] is False
    assert env["error"]["code"] == "nothing_to_commit"
    assert env["error"]["data"]["reason"] == "nothing_to_commit"


# ---------------------------------------------------------------------------
# 12 — auth_failed. _AuthRetryGroup (core.py) catches AuthenticationError from ANY
# subcommand in one place; under CliRunner, ui.is_interactive() is always False (no real
# tty), so the non-interactive branch (fail(ctx, "auth_failed", ...)) fires regardless of
# --output json — proving the same code path both modes actually share, rather than
# needing two different simulations.
# ---------------------------------------------------------------------------

def _make_push_401(monkeypatch, repo):
    from python.av_cli.client import AuthenticationError, VaultClient

    # Force unreachable for the setup commit — a real dev stack may genuinely be up on
    # localhost:8000 (Docker Desktop), and if it were, this commit would push for real
    # against it instead of queuing, leaving nothing pending for the `push` below to retry
    # (and, worse, writing test data into a real server). "No server configured" must never
    # be simulated by just not mocking anything.
    monkeypatch.setattr(VaultClient, "server_available", lambda self: False)
    (repo / "f.txt").write_text("v1")
    invoke("add", "f.txt")
    # Queues (same shape as the unreachable_queued test above) — gives `push` a pending
    # commit to retry.
    invoke("commit", "-m", "queued for auth-failed repro")
    monkeypatch.setattr(VaultClient, "server_available", lambda self: True)

    def _raise(*args, **kwargs):
        raise AuthenticationError("Server rejected the request (401)")

    # cmd_history.py does `from .core import *` at import time, so `flush_pending_push`
    # is a name bound in cmd_history's own module namespace by now — patching
    # core.flush_pending_push itself would not affect the already-resolved reference
    # push() actually calls.
    monkeypatch.setattr("python.av_cli.cmd_history.flush_pending_push", _raise)


def test_auth_failed_exits_12_text(repo, monkeypatch):
    _make_push_401(monkeypatch, repo)
    result = invoke("push")
    assert result.exit_code == 12, result.output
    assert "protected" in result.output.lower()


def test_auth_failed_exits_12_json(repo, monkeypatch):
    _make_push_401(monkeypatch, repo)
    result = invoke_json("push")
    assert result.exit_code == 12, result.output
    env = json.loads(result.output)
    assert env["ok"] is False
    assert env["error"]["code"] == "auth_failed"


# ---------------------------------------------------------------------------
# 13 — unreachable_queued: `commit`/`push` deliberately exit 0 when queued (queued is a
# SAFE, complete local outcome by design — AGENTS.md non-negotiable #3, and `av push`
# already behaves this way for "reachable": False). What WAS broken: `av --output json
# commit`'s "queued"/"queued_reason" fields were silently wrong (always false/None unless
# --no-upload) because the JSON result_sink fired before the push-or-queue logic ran and
# mutated the result it had already captured. Fixed by deferring that capture to the end
# of _finalize_commit (core.py) — this test proves the DATA is now accurate; exit 13
# itself is reserved for read-path commands where reachability is the primary outcome
# (av audit list / av webhooks list — see those modules' own fail("unreachable_queued")
# call sites, unaffected by this fix).
# ---------------------------------------------------------------------------

def test_commit_json_reports_queued_accurately_when_server_unreachable(repo, unreachable_client):
    (repo / "f.txt").write_text("v1")
    invoke("add", "f.txt")
    result = invoke_json("commit", "-m", "queued")
    assert result.exit_code == 0, result.output  # queued is not a failure
    env = json.loads(result.output)
    assert env["ok"] is True
    assert env["data"]["queued"] is True
    assert env["data"]["queued_reason"] == "server_unreachable"


# ---------------------------------------------------------------------------
# 14 — merge_conflict (both the real conflict path and pull's divergence path, which
# deliberately reuses this code — see cmd_sync.py)
# ---------------------------------------------------------------------------

def test_merge_conflict_exits_14(repo):
    (repo / "shared.txt").write_text("base")
    invoke("add", "shared.txt")
    invoke("commit", "-m", "base")
    invoke("branch", "feature")

    invoke("checkout", "feature")
    (repo / "shared.txt").write_text("feature")
    invoke("add", "shared.txt")
    invoke("commit", "-m", "feature edit")

    invoke("checkout", "main")
    (repo / "shared.txt").write_text("main")
    invoke("add", "shared.txt")
    invoke("commit", "-m", "main edit")

    result = invoke("merge", "feature")
    assert result.exit_code == 14, result.output


def test_merge_conflict_json_envelope_has_conflict_data(repo):
    (repo / "shared.txt").write_text("base")
    invoke("add", "shared.txt")
    invoke("commit", "-m", "base")
    invoke("branch", "feature")

    invoke("checkout", "feature")
    (repo / "shared.txt").write_text("feature")
    invoke("add", "shared.txt")
    invoke("commit", "-m", "feature edit")

    invoke("checkout", "main")
    (repo / "shared.txt").write_text("main")
    invoke("add", "shared.txt")
    invoke("commit", "-m", "main edit")

    result = invoke_json("merge", "feature")
    assert result.exit_code == 14, result.output
    env = json.loads(result.output)
    assert env["error"]["code"] == "merge_conflict"
    assert "shared.txt" in env["error"]["data"]["conflicts"]
    assert env["error"]["data"]["remediation"]


# ---------------------------------------------------------------------------
# 15 — validation (already correct pre-1.2.5 at call sites that used fail() directly;
# pinned here for completeness of the registry proof)
# ---------------------------------------------------------------------------

def test_validation_exits_15(repo):
    result = invoke("merge", "does-not-exist")
    assert result.exit_code == 15, result.output


# ---------------------------------------------------------------------------
# 16 — policy_denied (already correct pre-1.2.5)
# ---------------------------------------------------------------------------

def test_policy_denied_exits_16(repo):
    (repo / "m.txt").write_text("v1")
    invoke("add", "m.txt")
    invoke("commit", "-m", "baseline", "--metric", "val_loss=1.0")
    baseline = (repo / ".av" / "refs" / "heads" / "main").read_text().strip()

    invoke("policy", "set", "main", "val_loss", "<", "--baseline-ref", baseline)

    (repo / "m.txt").write_text("v2")
    invoke("add", "m.txt")
    invoke("commit", "-m", "regressed", "--metric", "val_loss=2.0")

    result = invoke("promote", "--into", "main")
    assert result.exit_code == 16, result.output


def test_policy_denied_exits_16_json(repo):
    """v1.3.1 fix (Probleme.md): the deny branch used to `click.secho(..., err=True)`
    unconditionally even in JSON mode, leaking human text after the envelope — never
    caught before because no prior test parsed a JSON-mode DENY as strict JSON.

    Note the envelope's `ok:true`/`error:null` here is the EXISTING, unchanged contract
    for this specific denial (it reports the decision via `data.allowed`, not
    `error.code`, unlike `av merge`'s policy denial which does go through `fail()`) —
    this test locks in the leak fix, not a reshaping of that envelope, which would be a
    separate, deliberately out-of-scope breaking-contract question (see Probleme.md)."""
    (repo / "m.txt").write_text("v1")
    invoke("add", "m.txt")
    invoke("commit", "-m", "baseline", "--metric", "val_loss=1.0")
    baseline = (repo / ".av" / "refs" / "heads" / "main").read_text().strip()

    invoke("policy", "set", "main", "val_loss", "<", "--baseline-ref", baseline)

    (repo / "m.txt").write_text("v2")
    invoke("add", "m.txt")
    invoke("commit", "-m", "regressed", "--metric", "val_loss=2.0")

    result = invoke_json("promote", "--into", "main")
    assert result.exit_code == 16, result.output
    env = json.loads(result.output)  # must parse cleanly as ONE JSON object
    assert env["data"]["allowed"] is False


# ---------------------------------------------------------------------------
# promote --dry-run (v1.3.0, todo.md item 12): exits 0 for BOTH decisions, touches
# nothing either way — a script branches on data.decision, not the exit code.
# ---------------------------------------------------------------------------

def _armed_repo_with_regressed_tip(repo):
    (repo / "m.txt").write_text("v1")
    invoke("add", "m.txt")
    invoke("commit", "-m", "baseline", "--metric", "val_loss=1.0")
    baseline = (repo / ".av" / "refs" / "heads" / "main").read_text().strip()
    invoke("policy", "set", "main", "val_loss", "<", "--baseline-ref", baseline)
    (repo / "m.txt").write_text("v2")
    invoke("add", "m.txt")
    invoke("commit", "-m", "regressed", "--metric", "val_loss=2.0")
    return baseline


def test_promote_dry_run_deny_exits_0_and_lands_nothing(repo):
    _armed_repo_with_regressed_tip(repo)
    head_before = (repo / ".av" / "refs" / "heads" / "main").read_text().strip()

    result = invoke_json("promote", "--into", "main", "--dry-run")
    assert result.exit_code == 0, result.output
    env = json.loads(result.output)
    assert env["data"]["dry_run"] is True
    assert env["data"]["decision"] == "deny"
    assert env["data"]["rule"].startswith("metric:val_loss")

    assert (repo / ".av" / "refs" / "heads" / "main").read_text().strip() == head_before, \
        "dry-run must not land anything even on what would be a DENY"


def _fake_registry_client(monkeypatch, get_response=None, post_response=None):
    """v1.3.1: a `VaultClient` subclass whose session returns canned responses instead of
    hitting a real socket — same pattern `test_sdk_run_start_registration_payload_includes_
    project_id` (test_av_sdk.py) already established for a reachable-but-fake server."""
    import python.av_cli.client as client_module

    class _FakeResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

        @property
        def text(self):
            return json.dumps(self._body)

    class _FakeSession:
        def get(self, url, params=None, timeout=None):
            return _FakeResponse(*get_response) if get_response else _FakeResponse(200, {})

        def post(self, url, json=None):
            return _FakeResponse(*post_response) if post_response else _FakeResponse(200, {})

    class _FakeClient(client_module.VaultClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.session = _FakeSession()

        def server_available(self) -> bool:
            return True

    monkeypatch.setattr(client_module, "VaultClient", _FakeClient)


# ---------------------------------------------------------------------------
# 17 — budget_exhausted (v1.3.1): `av budget consume` reports the spend it just recorded
# is now over some dimension's limit.
# ---------------------------------------------------------------------------

def test_budget_exhausted_exits_17(repo, monkeypatch):
    _fake_registry_client(monkeypatch, post_response=(200, {
        "id": "b1", "project_id": "p", "scope": "run", "scope_ref": "r1",
        "compute_seconds_limit": 10.0, "storage_bytes_limit": None, "step_limit": None,
        "compute_seconds_used": 15.0, "storage_bytes_used": 0, "steps_used": 0,
        "exhausted": True, "exceeded_dims": ["compute_seconds"],
    }))
    result = invoke("budget", "consume", "b1", "--compute-seconds", "15")
    assert result.exit_code == 17, result.output
    assert "exhausted" in result.output.lower()


def test_budget_exhausted_exits_17_json(repo, monkeypatch):
    _fake_registry_client(monkeypatch, post_response=(200, {
        "id": "b1", "project_id": "p", "scope": "run", "scope_ref": "r1",
        "compute_seconds_limit": 10.0, "storage_bytes_limit": None, "step_limit": None,
        "compute_seconds_used": 15.0, "storage_bytes_used": 0, "steps_used": 0,
        "exhausted": True, "exceeded_dims": ["compute_seconds"],
    }))
    result = invoke_json("budget", "consume", "b1", "--compute-seconds", "15")
    assert result.exit_code == 17, result.output
    env = json.loads(result.output)
    assert env["ok"] is False
    assert env["error"]["code"] == "budget_exhausted"
    assert env["error"]["data"]["exhausted"] is True  # the spend still recorded — carried in error.data


def test_budget_not_exhausted_exits_0(repo, monkeypatch):
    _fake_registry_client(monkeypatch, post_response=(200, {
        "id": "b1", "project_id": "p", "scope": "run", "scope_ref": "r1",
        "compute_seconds_limit": 100.0, "storage_bytes_limit": None, "step_limit": None,
        "compute_seconds_used": 5.0, "storage_bytes_used": 0, "steps_used": 0,
        "exhausted": False, "exceeded_dims": [],
    }))
    result = invoke("budget", "consume", "b1", "--compute-seconds", "5")
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# 19 — review_required (v1.3.1): `av improver promote`'s require_review gate denies with
# THIS code, not 16 (policy_denied) — "nobody has signed off yet" needs a different
# remediation ("get it reviewed") than a metric/signature mismatch.
# ---------------------------------------------------------------------------

def test_review_required_exits_19(repo, monkeypatch):
    _fake_registry_client(monkeypatch, get_response=(200, {"reviews": []}))
    invoke("improver", "policy", "set", "main", "--require-review")
    invoke("improver", "use", "cand-1")
    result = invoke("improver", "promote", "--into", "main")
    assert result.exit_code == 19, result.output


def test_review_required_exits_19_json(repo, monkeypatch):
    _fake_registry_client(monkeypatch, get_response=(200, {"reviews": []}))
    invoke("improver", "policy", "set", "main", "--require-review")
    invoke("improver", "use", "cand-1")
    result = invoke_json("improver", "promote", "--into", "main")
    assert result.exit_code == 19, result.output
    env = json.loads(result.output)
    assert env["data"]["allowed"] is False
    assert env["data"]["rule"] == "require_review"


def test_review_present_allows_promotion(repo, monkeypatch):
    _fake_registry_client(monkeypatch, get_response=(200, {
        "reviews": [{"decision": "approve"}], "critiques": []}))
    invoke("improver", "policy", "set", "main", "--require-review")
    invoke("improver", "use", "cand-1")
    result = invoke("improver", "promote", "--into", "main")
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# 18 — frozen (v1.3.1): `av promote` unconditionally checks freeze state (after any
# promote-policy evaluation, before landing) regardless of whether a promote policy is
# armed — freeze and the model-gate policy are independent, and freeze wins even over
# --force on the policy itself.
# ---------------------------------------------------------------------------

def test_frozen_exits_18(repo, monkeypatch):
    _fake_registry_client(monkeypatch, get_response=(200, {"project_id": "p", "frozen": True,
                                                          "reason": "incident"}))
    (repo / "m.txt").write_text("v1")
    invoke("add", "m.txt")
    invoke("commit", "-m", "baseline")

    result = invoke("promote", "--into", "main")
    assert result.exit_code == 18, result.output
    assert "frozen" in result.output.lower()


def test_frozen_exits_18_json(repo, monkeypatch):
    _fake_registry_client(monkeypatch, get_response=(200, {"project_id": "p", "frozen": True,
                                                          "reason": "incident"}))
    (repo / "m.txt").write_text("v1")
    invoke("add", "m.txt")
    invoke("commit", "-m", "baseline")

    result = invoke_json("promote", "--into", "main")
    assert result.exit_code == 18, result.output
    env = json.loads(result.output)
    assert env["ok"] is False
    assert env["error"]["code"] == "frozen"


def test_not_frozen_promote_is_unaffected(repo, monkeypatch):
    """Compatibility invariant: an unfrozen (or unreachable) project must not change
    `av promote`'s existing no-policy-armed behavior."""
    _fake_registry_client(monkeypatch, get_response=(200, {"project_id": "p", "frozen": False,
                                                          "reason": None}))
    (repo / "m.txt").write_text("v1")
    invoke("add", "m.txt")
    invoke("commit", "-m", "baseline")

    result = invoke("promote", "--into", "main")
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# 20 — scope_denied (v1.3.1): the server's 403 {"error":"scope_denied"} from
# `POST /api/freeze/{project_id}` (the `admin`-scoped route) maps to this exit code.
# ---------------------------------------------------------------------------

def test_scope_denied_exits_20(repo, monkeypatch):
    _fake_registry_client(monkeypatch, post_response=(403, {
        "detail": {"error": "scope_denied", "required_scope": "admin"}}))
    result = invoke("freeze", "on", "--reason", "test")
    assert result.exit_code == 20, result.output
    assert "scope" in result.output.lower()


def test_scope_denied_exits_20_json(repo, monkeypatch):
    _fake_registry_client(monkeypatch, post_response=(403, {
        "detail": {"error": "scope_denied", "required_scope": "admin"}}))
    result = invoke_json("freeze", "on")
    assert result.exit_code == 20, result.output
    env = json.loads(result.output)
    assert env["ok"] is False
    assert env["error"]["code"] == "scope_denied"


def test_promote_dry_run_allow_exits_0_and_lands_nothing(repo):
    (repo / "m.txt").write_text("v1")
    invoke("add", "m.txt")
    invoke("commit", "-m", "baseline", "--metric", "val_loss=1.0")
    baseline = (repo / ".av" / "refs" / "heads" / "main").read_text().strip()
    invoke("policy", "set", "main", "val_loss", "<", "--baseline-ref", baseline)
    (repo / "m.txt").write_text("v2")
    invoke("add", "m.txt")
    invoke("commit", "-m", "improved", "--metric", "val_loss=0.5")
    head_before = (repo / ".av" / "refs" / "heads" / "main").read_text().strip()

    result = invoke_json("promote", "--into", "main", "--dry-run")
    assert result.exit_code == 0, result.output
    env = json.loads(result.output)
    assert env["data"]["decision"] == "allow"

    assert (repo / ".av" / "refs" / "heads" / "main").read_text().strip() == head_before, \
        "dry-run must not land anything even on what would be an ALLOW"
