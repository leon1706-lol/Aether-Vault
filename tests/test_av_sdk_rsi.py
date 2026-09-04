"""av_sdk.Repo's RSI surfaces (v1.3.1, WP-37) — the SDK methods added to close the "every
new surface reachable from av_sdk.Repo" requirement for R1-R5. Reuses the same stateful
in-memory fake-registry technique as tests/test_improver.py (a fresh one here, since this
file also needs reviews/critiques/budgets/lessons/blackboard, which that one doesn't
track) so the SDK methods' request/response handling is proven end to end, not just
import-checked. Live server-side route behavior is proven in tests/test_server.py; CLI
parity for each of these commands is proven in their own tests/test_*.py files — this
file is specifically about the SDK layer reusing the SAME plain functions (or replicating
the same minimal wire sequence) rather than diverging from them.
"""
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


class _FakeRegistry:
    def __init__(self):
        self.improvers = {}
        self.change_sets = {}
        self.canary_results = []
        self.reviews = []
        self.critiques = {}
        self.budgets = {}
        self.lessons = []
        self.objects = set()
        self._n = 0

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}-{self._n}"

    def create_improver(self, body):
        iid = body.get("id") or self._id("improver")
        self.improvers[iid] = {**body, "id": iid, "created_at": "2026-01-01T00:00:00"}
        return 201, {"status": "created", "id": iid}

    def create_change_set(self, body):
        cs_id = body.get("id") or self._id("cs")
        self.change_sets[cs_id] = {**body, "id": cs_id, "status": "proposed"}
        return 201, {"status": "created", "id": cs_id}

    def transition(self, cs_id, new_status):
        cs = self.change_sets.get(cs_id)
        if cs is None:
            return 404, {"detail": "not found"}
        cs["status"] = new_status
        return 200, {"status": "updated", "id": cs_id, "new_status": new_status}


def _fake_client(monkeypatch, reg: _FakeRegistry):
    import av_cli.client as client_module

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
        identity = "alice"

        def get(self, url, params=None, timeout=None):
            params = params or {}
            if "/api/improvers/" in url and url.endswith("/lineage"):
                iid = url.rsplit("/", 2)[-2]
                chain, node, seen = [], reg.improvers.get(iid), set()
                while node and node["id"] not in seen:
                    seen.add(node["id"])
                    chain.append(node)
                    node = reg.improvers.get(node.get("parent_id"))
                return _FakeResponse(200, {"lineage": chain})
            if "/api/improvers/" in url:
                row = reg.improvers.get(url.rsplit("/", 1)[-1])
                return _FakeResponse(200, row) if row else _FakeResponse(404, {})
            if "/api/change-sets/" in url:
                row = reg.change_sets.get(url.rsplit("/", 1)[-1])
                return _FakeResponse(200, row) if row else _FakeResponse(404, {})
            if url.endswith("/api/canary-results"):
                rows = [r for r in reg.canary_results if r["improver_id"] == params.get("improver_id")]
                return _FakeResponse(200, {"canary_results": rows})
            if url.endswith("/api/reviews"):
                rows = [r for r in reg.reviews
                       if r["target_type"] == params.get("target_type")
                       and r["target_id"] == params.get("target_id")]
                return _FakeResponse(200, {"reviews": rows})
            if url.endswith("/api/critiques"):
                rows = [c for c in reg.critiques.values()
                       if c["target_type"] == params.get("target_type")
                       and c["target_id"] == params.get("target_id")
                       and (not params.get("status") or c["status"] == params["status"])]
                return _FakeResponse(200, {"critiques": rows})
            if "/api/budgets/" in url:
                row = reg.budgets.get(url.rsplit("/", 1)[-1])
                return _FakeResponse(200, row) if row else _FakeResponse(404, {})
            if url.endswith("/api/lessons/latest"):
                return (_FakeResponse(200, reg.lessons[-1]) if reg.lessons
                       else _FakeResponse(404, {}))
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
                return _FakeResponse(201, {"status": "recorded"})
            if url.endswith("/api/reviews"):
                row = {**body, "id": reg._id("review"), "reviewer": self.identity}
                reg.reviews.append(row)
                return _FakeResponse(201, row)
            if url.endswith("/api/critiques"):
                cid = reg._id("crit")
                row = {**body, "id": cid, "status": "open"}
                reg.critiques[cid] = row
                return _FakeResponse(201, row)
            if url.endswith("/resolve") and "/api/critiques/" in url:
                cid = url.split("/api/critiques/")[1].split("/resolve")[0]
                reg.critiques[cid]["status"] = "resolved"
                return _FakeResponse(200, reg.critiques[cid])
            if url.endswith("/waive") and "/api/critiques/" in url:
                cid = url.split("/api/critiques/")[1].split("/waive")[0]
                reg.critiques[cid]["status"] = "waived"
                return _FakeResponse(200, reg.critiques[cid])
            if url.endswith("/api/budgets"):
                bid = reg._id("budget")
                row = {**body, "id": bid, "compute_seconds_used": 0, "storage_bytes_used": 0,
                      "steps_used": 0}
                reg.budgets[bid] = row
                return _FakeResponse(201, row)
            if url.endswith("/consume"):
                bid = url.split("/api/budgets/")[1].split("/consume")[0]
                row = reg.budgets[bid]
                row["compute_seconds_used"] += body.get("compute_seconds", 0)
                limit = row.get("compute_seconds_limit")
                exhausted = limit is not None and row["compute_seconds_used"] > limit
                return _FakeResponse(200, {**row, "exhausted": exhausted,
                                           "exceeded_dims": ["compute_seconds"] if exhausted else []})
            if url.endswith("/api/lessons"):
                lid = reg._id("lessons")
                row = {**body, "id": lid}
                reg.lessons.append(row)
                return _FakeResponse(201, row)
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
    return reg


@pytest.fixture
def fake_reg(monkeypatch):
    return _fake_client(monkeypatch, _FakeRegistry())


# ---------------------------------------------------------------------------
# The core RSI loop, end to end through the SDK: propose -> approve -> apply ->
# canary -> promote (denied, no review yet) -> review -> promote (allowed) -> lessons.
# ---------------------------------------------------------------------------

def test_full_improver_loop_via_sdk(repo, fake_reg, tmp_path):
    code_file = tmp_path / "agent.py"
    code_file.write_text("print('v1')", encoding="utf-8")

    with Repo(repo) as r:
        base = r.improver_register(code_paths=[str(code_file)], sign=False)
        assert base["signed"] is False
        assert r.improver_current() == base["id"]

        cs = r.improver_propose("--- a\n+++ b\n", "make it faster", risk="low")
        assert cs["improver_id"] == base["id"]

        r.improver_review(cs["id"], "approved")
        applied = r.improver_apply(cs["id"])
        assert applied["previous_improver_id"] == base["id"]
        new_id = applied["new_improver_id"]
        assert r.improver_current() == new_id

        shown = r.improver_show(new_id)
        assert shown["id"] == new_id
        lineage = r.improver_lineage(new_id)
        assert [n["id"] for n in lineage["lineage"]] == [new_id, base["id"]]

        # Arm require_review via the CLI (no SDK surface for improver policy — the
        # policy-arming verbs are local-only config, not a network operation).
        from click.testing import CliRunner as _CR
        assert _CR().invoke(cli, ["improver", "policy", "set", "main",
                                  "--require-review"]).exit_code == 0

        denied = r.improver_promote(new_id, dry_run=True)
        assert denied["decision"] == "deny"
        with pytest.raises(SDKError) as ei:
            r.improver_promote(new_id)
        assert ei.value.code == "review_required"
        assert ei.value.exit_code == 19

        r.review_submit(new_id, "approve", target_type="improver")
        promoted = r.improver_promote(new_id)
        assert promoted["allowed"] is True
        assert promoted["candidate"] == new_id

        rolled_back = r.improver_rollback()
        assert rolled_back["active_improver_id"] == base["id"]

        lesson = r.lessons_update({"beliefs": ["smaller LR helped"]})
        assert lesson["object_id"]
        shown_lesson = r.lessons_show()
        assert shown_lesson["document"]["beliefs"] == ["smaller LR helped"]


def test_promote_denied_without_any_policy_is_allowed_by_default(repo, fake_reg):
    with Repo(repo) as r:
        base = r.improver_register(sign=False)
        result = r.improver_promote(base["id"])
        assert result["allowed"] is True
        assert result["rule"] is None


def test_critique_blocks_then_resolves(repo, fake_reg):
    with Repo(repo) as r:
        base = r.improver_register(sign=False)
        crit = r.critique_add(base["id"], "untested", target_type="improver")
        assert crit["status"] == "open"
        resolved = r.critique_finalize(crit["id"], "resolve", resolution="added tests")
        assert resolved["status"] == "resolved"

        crit2 = r.critique_add(base["id"], "risky", target_type="improver")
        waived = r.critique_finalize(crit2["id"], "waive", resolution="accepted risk")
        assert waived["status"] == "waived"


def test_canary_run_reports_and_reflects_head_metrics(repo, fake_reg, tmp_path):
    with Repo(repo) as r:
        (repo / "m.txt").write_text("x", encoding="utf-8")
        r.add("m.txt")
        r.commit("c1", metrics={"val_loss": 0.4})
        base = r.improver_register(sign=False)

        from python.av_cli import casobj

        suite = {"kind": "canary_suite", "name": "core",
                 "checks": [{"name": "loss_ok", "metric": "val_loss", "op": "<=", "threshold": 0.6}]}
        object_id = casobj.write_object(repo, suite)
        reg_path = repo / ".av" / "canaries.json"
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text(json.dumps({"core": object_id}), encoding="utf-8")

        result = r.canary_run("core", improver_id=base["id"])
        assert result["passed"] is True
        assert result["reported"] is True


def test_budget_consume_raises_typed_error_on_exhaustion(repo, fake_reg):
    with Repo(repo) as r:
        budget = r.budget_set("run-1", compute_seconds_limit=10.0)
        r.budget_consume(budget["id"], compute_seconds=5.0)
        with pytest.raises(SDKError) as ei:
            r.budget_consume(budget["id"], compute_seconds=10.0)
        assert ei.value.code == "budget_exhausted"
        assert ei.value.exit_code == 17
        shown = r.budget_show(budget["id"])
        assert shown["compute_seconds_used"] == 15.0


def test_freeze_status_and_set_via_sdk(repo, monkeypatch):
    import av_cli.client as client_module

    state = {"frozen": False, "reason": None}

    class _FakeResponse:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    class _FakeSession:
        def get(self, url, params=None, timeout=None):
            return _FakeResponse(dict(state))

        def post(self, url, json=None):
            state["frozen"], state["reason"] = json["frozen"], json.get("reason")
            return _FakeResponse(dict(state))

    class _FreezeClient(client_module.VaultClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.session = _FakeSession()

        def server_available(self) -> bool:
            return True

    monkeypatch.setattr(client_module, "VaultClient", _FreezeClient)
    with Repo(repo) as r:
        assert r.freeze_status()["frozen"] is False
        r.freeze_set(True, reason="incident")
        assert r.freeze_status() == {"frozen": True, "reason": "incident"}
        r.freeze_set(False)
        assert r.freeze_status()["frozen"] is False


def test_unreachable_client_gives_typed_unreachable_error(repo, unreachable_client):
    with Repo(repo) as r:
        with pytest.raises(SDKError) as ei:
            r.improver_register()
        assert ei.value.code == "unreachable_queued"
        assert ei.value.exit_code == 13
