"""examples/rsi_loop/agent.py — the deterministic reference agent (todo.md item 46 /
plan WP-40) — run stack-free against an in-memory fake registry. Proves the narrative's
LOGIC (every step happens in the right order, every denial is the right one) without any
live infrastructure; the same function is what a real live run calls (see the module's
own README.md).
"""
import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "rsi_loop"))
from agent import run_rsi_loop  # noqa: E402


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
        self.budgets = {}
        self.lessons = []
        self.blackboard = []
        self.causal_links = []
        self.strategy = []
        self.objects = set()
        self._n = 0

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}-{self._n}"


def _install_fake(monkeypatch, reg: _FakeRegistry):
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
        def get(self, url, params=None, timeout=None):
            params = params or {}
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
                       if r.get("target_type") == params.get("target_type")
                       and r.get("target_id") == params.get("target_id")]
                return _FakeResponse(200, {"reviews": rows})
            if url.endswith("/api/critiques"):
                return _FakeResponse(200, {"critiques": []})
            if "/api/budgets/" in url:
                row = reg.budgets.get(url.rsplit("/", 1)[-1])
                return _FakeResponse(200, row) if row else _FakeResponse(404, {})
            if url.endswith("/api/search/runs"):
                return _FakeResponse(200, {"matches": []})
            return _FakeResponse(404, {})

        def post(self, url, json=None):
            body = json or {}
            if url.endswith("/api/improvers"):
                iid = body.get("id") or reg._id("improver")
                reg.improvers[iid] = {**body, "id": iid, "created_at": "2026-01-01T00:00:00"}
                return _FakeResponse(201, {"status": "created", "id": iid})
            if url.endswith("/api/change-sets"):
                cs_id = body.get("id") or reg._id("cs")
                reg.change_sets[cs_id] = {**body, "id": cs_id, "status": "proposed"}
                return _FakeResponse(201, {"status": "created", "id": cs_id})
            if url.endswith("/status"):
                cs_id = url.split("/api/change-sets/")[1].split("/status")[0]
                reg.change_sets[cs_id]["status"] = body.get("status")
                return _FakeResponse(200, {"status": "updated", "id": cs_id})
            if url.endswith("/api/canary-results"):
                reg.canary_results.append({**body, "created_at": "2026-01-01T00:00:00"})
                return _FakeResponse(201, {"status": "recorded"})
            if url.endswith("/api/reviews"):
                row = {**body, "id": reg._id("review")}
                reg.reviews.append(row)
                return _FakeResponse(201, row)
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
            if url.endswith("/api/blackboard"):
                row = {**body, "id": reg._id("claim")}
                reg.blackboard.append(row)
                return _FakeResponse(201, row)
            if url.endswith("/api/causal-links"):
                row = {**body, "id": reg._id("link")}
                reg.causal_links.append(row)
                return _FakeResponse(201, row)
            if url.endswith("/api/strategy"):
                row = {**body, "id": reg._id("strategy")}
                reg.strategy.append(row)
                return _FakeResponse(201, row)
            # sandbox job telemetry (best-effort — cmd_sandbox._report_job ignores the
            # response either way) and any other unmodeled route: safe 404 fallback.
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

        def push_commit(self, commit_data: dict) -> bool:
            return False  # the training commit queues locally — its content is what
                          # canary_run reads, and that's purely a local file read.

        def update_ref(self, ref_name, commit_hash, expected_hash=None) -> bool:
            return False

        def batch_check_objects(self, sha256_hashes):
            return set()

    monkeypatch.setattr(client_module, "VaultClient", _FakeClient)
    return reg


def _register_canary_suite(repo_path):
    from python.av_cli import casobj

    suite = {"kind": "canary_suite", "name": "core-capability",
             "checks": [{"name": "loss_ok", "metric": "val_loss", "op": "<=", "threshold": 0.6}]}
    object_id = casobj.write_object(repo_path, suite)
    reg_path = repo_path / ".av" / "canaries.json"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps({"core-capability": object_id}), encoding="utf-8")


def test_rsi_loop_end_to_end(repo, monkeypatch):
    reg = _install_fake(monkeypatch, _FakeRegistry())
    _register_canary_suite(repo)

    steps = run_rsi_loop(repo)
    by_step = {s["step"]: s for s in steps}

    assert by_step["commit"]["hash"]
    assert by_step["improver_register"]["id"]
    assert by_step["improver_propose"]["id"]
    assert by_step["improver_apply"]["new_improver_id"]

    # The sandboxed execution actually ran (local driver, real subprocess).
    assert by_step["sandbox_run"]["state"] == "succeeded"
    assert by_step["sandbox_run"]["exit_code"] == 0

    # The canary passed (val_loss=0.5 <= 0.6).
    assert by_step["canary_run"]["passed"] is True

    # The FIRST promotion attempt was genuinely denied on review_required, not skipped.
    assert "unexpectedly_allowed" not in by_step["improver_promote_denied"]
    assert by_step["improver_promote_denied"]["code"] == "review_required"
    assert by_step["improver_promote_denied"]["exit_code"] == 19

    # The SECOND attempt, after review, actually landed.
    assert by_step["improver_promote_allowed"]["candidate"] == by_step["improver_apply"]["new_improver_id"]

    # The budget genuinely exhausted, not silently no-opped.
    assert "unexpectedly_not_exhausted" not in by_step["budget_exhausted"]
    assert by_step["budget_exhausted"]["code"] == "budget_exhausted"
    assert by_step["budget_exhausted"]["exit_code"] == 17

    assert by_step["lessons_update"]["object_id"]
    assert "match_count" in by_step["search_runs"]

    # Registry-side proof that every write actually landed, not just the SDK's own
    # return values.
    assert len(reg.change_sets) == 1
    assert reg.change_sets[by_step["improver_propose"]["id"]]["status"] == "applied"
    assert len(reg.reviews) == 1
    assert len(reg.canary_results) == 1
    assert len(reg.blackboard) == 1
    assert len(reg.causal_links) == 1
    assert len(reg.strategy) == 1
    assert len(reg.lessons) == 1


def test_rsi_loop_prints_when_a_print_fn_is_given(repo, monkeypatch, capsys):
    _install_fake(monkeypatch, _FakeRegistry())
    _register_canary_suite(repo)

    run_rsi_loop(repo, print_fn=print)
    out = capsys.readouterr().out
    assert "RSI loop complete." in out
    assert "DENIED" in out
    assert "PROMOTED" in out
    assert "STOPPED" in out
