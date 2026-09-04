"""av improver — versioned improver artifacts, self-edit lifecycle, dual-gate promotion
(v1.3.1, RSI R1).

Uses a small in-memory FAKE registry (improvers + change-sets + canary-results) behind
`VaultClient`, the same fake-session-subclass technique tests/test_exit_codes.py and
tests/test_freeze.py already establish — proves the CLI's request/response handling and
local state management without a live server. The real server-side route behavior
(idempotent create, 422s, the change-set transition state machine) is proven live in
tests/test_server.py.
"""
import json

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def invoke_json(*args):
    return CliRunner().invoke(cli, ["--output", "json", *args])


class _FakeRegistry:
    """Minimal in-memory backing store for the improver/change-set/canary endpoints this
    module's commands call."""

    def __init__(self):
        self.improvers = {}
        self.change_sets = {}
        self.canary_results = []
        self.objects = set()
        self._next_cs = 0

    def create_improver(self, body):
        iid = body.get("id") or f"improver-{len(self.improvers)}"
        if iid in self.improvers:
            return 200, {"status": "exists", "id": iid}
        self.improvers[iid] = {
            "id": iid, "project_id": body["project_id"],
            "manifest_object_id": body["manifest_object_id"],
            "parent_id": body.get("parent_id"), "created_by": None,
            "created_at": "2026-01-01T00:00:00",
        }
        return 201, {"status": "created", "id": iid}

    def create_change_set(self, body):
        cs_id = body.get("id") or f"cs-{self._next_cs}"
        self._next_cs += 1
        self.change_sets[cs_id] = {
            "id": cs_id, "project_id": body["project_id"],
            "improver_id": body.get("improver_id"), "object_id": body["object_id"],
            "status": "proposed", "risk": body.get("risk"),
            "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
        }
        return 201, {"status": "created", "id": cs_id}

    _TRANSITIONS = {
        "proposed": {"approved", "rejected"},
        "approved": {"applied", "rejected"},
        "applied": {"rolled_back"},
        "rejected": set(), "rolled_back": set(),
    }

    def transition(self, cs_id, new_status):
        cs = self.change_sets.get(cs_id)
        if cs is None:
            return 404, {"detail": "not found"}
        if new_status not in self._TRANSITIONS.get(cs["status"], set()):
            return 422, {"detail": f"cannot go from {cs['status']} to {new_status}"}
        cs["status"] = new_status
        return 200, {"status": "updated", "id": cs_id, "new_status": new_status}


def _fake_client(monkeypatch, reg: _FakeRegistry):
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
            params = params or {}
            if url.endswith("/lineage"):
                improver_id = url.rsplit("/", 2)[-2]
                chain = []
                seen = set()
                node = reg.improvers.get(improver_id)
                while node and node["id"] not in seen:
                    seen.add(node["id"])
                    chain.append(node)
                    node = reg.improvers.get(node.get("parent_id"))
                return _FakeResponse(200, {"improver_id": improver_id, "lineage": chain,
                                           "next_cursor": None})
            if "/api/improvers/" in url:
                iid = url.rsplit("/", 1)[-1]
                row = reg.improvers.get(iid)
                return _FakeResponse(200, row) if row else _FakeResponse(404, {})
            if url.endswith("/api/improvers"):
                rows = [r for r in reg.improvers.values()
                       if r["project_id"] == params.get("project_id")]
                return _FakeResponse(200, {"improvers": rows, "total": len(rows)})
            if "/api/change-sets/" in url:
                cs_id = url.rsplit("/", 1)[-1]
                row = reg.change_sets.get(cs_id)
                return _FakeResponse(200, row) if row else _FakeResponse(404, {})
            if url.endswith("/api/canary-results"):
                rows = [r for r in reg.canary_results if r["improver_id"] == params.get("improver_id")]
                rows = sorted(rows, key=lambda r: r["created_at"], reverse=True)
                return _FakeResponse(200, {"canary_results": rows[:params.get("limit", 50)]})
            return _FakeResponse(404, {})

        def post(self, url, json=None):
            body = json or {}
            if url.endswith("/api/improvers"):
                status, resp = reg.create_improver(body)
                return _FakeResponse(status, resp)
            if url.endswith("/api/change-sets"):
                status, resp = reg.create_change_set(body)
                return _FakeResponse(status, resp)
            if url.endswith("/status"):
                cs_id = url.split("/api/change-sets/")[1].split("/status")[0]
                status, resp = reg.transition(cs_id, body.get("status"))
                return _FakeResponse(status, resp)
            if url.endswith("/api/canary-results"):
                reg.canary_results.append({**body, "created_at": "2026-01-01T00:00:00"})
                return _FakeResponse(201, {"status": "recorded", "passed": body.get("passed")})
            return _FakeResponse(404, {})

    class _FakeClient(client_module.VaultClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.session = _FakeSession()

        def server_available(self) -> bool:
            return True

        def upload_object(self, file_path, sha256_hash, known_missing=False) -> bool:
            reg.objects.add(sha256_hash)
            return True

        def download_object(self, sha256_hash, dest_path) -> bool:
            return sha256_hash in reg.objects

    monkeypatch.setattr(client_module, "VaultClient", _FakeClient)


@pytest.fixture
def fake_reg(monkeypatch):
    reg = _FakeRegistry()
    _fake_client(monkeypatch, reg)
    return reg


# ---------------------------------------------------------------------------
# Registration, local pointer state
# ---------------------------------------------------------------------------

def test_register_without_server_queues(repo, unreachable_client):
    result = invoke("improver", "register")
    assert result.exit_code == 13, result.output  # unreachable_queued


def test_register_sets_current_pointer(repo, fake_reg):
    result = invoke_json("improver", "register")
    assert result.exit_code == 0, result.output
    env = json.loads(result.output)
    iid = env["data"]["id"]
    assert (repo / ".av" / "improver" / "current").read_text(encoding="utf-8").strip() == iid


def test_register_with_code_files_hashes_them(repo, fake_reg):
    (repo / "agent.py").write_text("print('hi')", encoding="utf-8")
    result = invoke_json("improver", "register", "--code", "agent.py")
    env = json.loads(result.output)
    assert env["data"]["code_files"] == 1


def test_init_is_a_register_alias(repo, fake_reg):
    result = invoke_json("improver", "init")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["parent_id"] is None


def test_current_reports_none_before_any_registration(repo):
    result = invoke_json("improver", "current")
    assert json.loads(result.output)["data"]["id"] is None


def test_use_sets_pointer_without_validating_against_registry(repo):
    result = invoke_json("improver", "use", "some-id")
    assert result.exit_code == 0, result.output
    result2 = invoke_json("improver", "current")
    assert json.loads(result2.output)["data"]["id"] == "some-id"


def test_lineage_walks_parent_chain(repo, fake_reg):
    r1 = json.loads(invoke_json("improver", "register").output)["data"]
    r2 = json.loads(invoke_json("improver", "register", "--parent", r1["id"]).output)["data"]

    result = invoke_json("improver", "lineage", r2["id"])
    chain_ids = [n["id"] for n in json.loads(result.output)["data"]["lineage"]]
    assert chain_ids == [r2["id"], r1["id"]]


def test_list_marks_the_current_pointer(repo, fake_reg):
    reg = json.loads(invoke_json("improver", "register").output)["data"]
    result = invoke("improver", "list")
    assert f"*{reg['id'][:8]}" in result.output.replace(" ", "")


# ---------------------------------------------------------------------------
# Self-edit proposals: propose -> review -> apply -> rollback
# ---------------------------------------------------------------------------

def test_propose_review_apply_rollback_lifecycle(repo, fake_reg, tmp_path):
    base = json.loads(invoke_json("improver", "register").output)["data"]

    diff_file = tmp_path / "change.diff"
    diff_file.write_text("--- a\n+++ b\n", encoding="utf-8")
    propose_result = invoke_json("improver", "propose", "--diff", str(diff_file),
                                 "--rationale", "fix a bug", "--risk", "low")
    assert propose_result.exit_code == 0, propose_result.output
    cs_id = json.loads(propose_result.output)["data"]["id"]
    assert fake_reg.change_sets[cs_id]["status"] == "proposed"

    # Applying before approval must fail cleanly.
    early_apply = invoke_json("improver", "apply", cs_id)
    assert early_apply.exit_code == 15, early_apply.output

    approve_result = invoke_json("improver", "review", cs_id, "--approve")
    assert approve_result.exit_code == 0, approve_result.output
    assert fake_reg.change_sets[cs_id]["status"] == "approved"

    apply_result = invoke_json("improver", "apply", cs_id)
    assert apply_result.exit_code == 0, apply_result.output
    applied = json.loads(apply_result.output)["data"]
    assert fake_reg.change_sets[cs_id]["status"] == "applied"
    assert applied["previous_improver_id"] == base["id"]

    new_current = invoke_json("improver", "current")
    assert json.loads(new_current.output)["data"]["id"] == applied["new_improver_id"]
    assert (repo / ".av" / "improver" / "last_good").read_text(encoding="utf-8").strip() == base["id"]

    rollback_result = invoke_json("improver", "rollback")
    assert rollback_result.exit_code == 0, rollback_result.output
    assert json.loads(rollback_result.output)["data"]["active_improver_id"] == base["id"]
    assert json.loads(invoke_json("improver", "current").output)["data"]["id"] == base["id"]


def test_review_requires_approve_or_reject(repo, fake_reg):
    result = invoke("improver", "review", "some-cs")
    assert result.exit_code == 15, result.output


def test_rollback_with_no_target_and_no_last_good_fails(repo):
    result = invoke("improver", "rollback")
    assert result.exit_code == 15, result.output


def test_rollback_to_explicit_id_ignores_last_good(repo):
    (repo / ".av" / "improver").mkdir(parents=True)
    (repo / ".av" / "improver" / "last_good").write_text("wrong-one", encoding="utf-8")
    result = invoke_json("improver", "rollback", "--to", "explicit-id")
    assert json.loads(result.output)["data"]["active_improver_id"] == "explicit-id"


# ---------------------------------------------------------------------------
# Improver-gate policy + dual-gate promote
# ---------------------------------------------------------------------------

def test_improver_policy_set_list_remove(repo):
    invoke("improver", "policy", "set", "main", "--require-canaries")
    listed = invoke_json("improver", "policy", "list")
    assert json.loads(listed.output)["data"]["policies"]["main"]["require_canaries"] is True

    invoke("improver", "policy", "remove", "main")
    listed2 = invoke_json("improver", "policy", "list")
    assert listed2.exit_code == 0
    assert json.loads(listed2.output)["data"]["policies"] == {}


def test_improver_policy_set_requires_at_least_one_flag(repo):
    result = invoke("improver", "policy", "set", "main")
    assert result.exit_code == 15, result.output


def test_promote_with_no_policy_armed_allows(repo, fake_reg):
    reg = json.loads(invoke_json("improver", "register").output)["data"]
    result = invoke_json("improver", "promote", reg["id"], "--into", "main")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["allowed"] is True


def test_promote_denied_without_passing_canary(repo, fake_reg):
    reg = json.loads(invoke_json("improver", "register").output)["data"]
    invoke("improver", "policy", "set", "main", "--require-canaries")

    result = invoke("improver", "promote", reg["id"], "--into", "main")
    assert result.exit_code == 16, result.output


def test_promote_allowed_with_passing_canary(repo, fake_reg):
    reg = json.loads(invoke_json("improver", "register").output)["data"]
    invoke("improver", "policy", "set", "main", "--require-canaries")
    fake_reg.canary_results.append({"improver_id": reg["id"], "passed": True,
                                    "created_at": "2026-01-01T00:00:01"})

    result = invoke_json("improver", "promote", reg["id"], "--into", "main")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["allowed"] is True
    promoted = (repo / ".av" / "improver" / "promoted" / "main")
    assert promoted.read_text(encoding="utf-8").strip() == reg["id"]


def test_promote_dry_run_deny_lands_nothing(repo, fake_reg):
    reg = json.loads(invoke_json("improver", "register").output)["data"]
    invoke("improver", "policy", "set", "main", "--require-canaries")

    result = invoke_json("improver", "promote", reg["id"], "--into", "main", "--dry-run")
    assert result.exit_code == 0, result.output  # dry-run always exits 0
    env = json.loads(result.output)
    assert env["data"]["decision"] == "deny"
    assert not (repo / ".av" / "improver" / "promoted" / "main").exists()


def test_promote_force_bypasses_policy(repo, fake_reg):
    reg = json.loads(invoke_json("improver", "register").output)["data"]
    invoke("improver", "policy", "set", "main", "--require-canaries")

    result = invoke_json("improver", "promote", reg["id"], "--into", "main", "--force")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["rule"] == "force"


def test_promote_with_no_candidate_and_no_pointer_fails(repo):
    result = invoke("improver", "promote")
    assert result.exit_code == 15, result.output
