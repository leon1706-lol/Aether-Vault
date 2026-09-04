"""av policy pack — signed, hash-chained, append-only policy publication log (v1.3.1).

Fake-registry technique shared with tests/test_improver.py; the real server-side
chain_hash computation and prev_id validation are proven live in tests/test_server.py.
This file re-derives the expected chain_hash the same way the server does
(sha256(f"{prev_id or ''}:{object_id}")) to prove the CLI round-trips it correctly.
"""
import hashlib
import json

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def invoke_json(*args):
    return CliRunner().invoke(cli, ["--output", "json", *args])


class _FakePackRegistry:
    def __init__(self):
        self.packs = {}
        self.objects = set()
        self._n = 0

    def latest(self, project_id):
        rows = [p for p in self.packs.values() if p["project_id"] == project_id]
        return max(rows, key=lambda p: p["created_at"]) if rows else None

    def create(self, body):
        pack_id = body.get("id") or f"pack-{self._n}"
        self._n += 1
        chain_hash = hashlib.sha256(
            f"{body.get('prev_id') or ''}:{body['object_id']}".encode()
        ).hexdigest()
        row = {"id": pack_id, "project_id": body["project_id"], "object_id": body["object_id"],
              "prev_id": body.get("prev_id"), "chain_hash": chain_hash,
              "published_by": None, "created_at": f"2026-01-01T00:00:{self._n:02d}"}
        self.packs[pack_id] = row
        return 201, row


def _fake_client(monkeypatch, reg: _FakePackRegistry):
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
            if url.endswith("/latest"):
                row = reg.latest(params.get("project_id"))
                return _FakeResponse(200, row) if row else _FakeResponse(404, {})
            if "/api/policy-packs/" in url:
                pack_id = url.rsplit("/", 1)[-1]
                row = reg.packs.get(pack_id)
                return _FakeResponse(200, row) if row else _FakeResponse(404, {})
            if url.endswith("/api/policy-packs"):
                rows = [p for p in reg.packs.values() if p["project_id"] == params.get("project_id")]
                rows = sorted(rows, key=lambda p: p["created_at"], reverse=True)
                return _FakeResponse(200, {"policy_packs": rows[:params.get("limit", 50)]})
            return _FakeResponse(404, {})

        def post(self, url, json=None):
            if url.endswith("/api/policy-packs"):
                status, row = reg.create(json)
                return _FakeResponse(status, row)
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
def fake_packs(monkeypatch):
    reg = _FakePackRegistry()
    _fake_client(monkeypatch, reg)
    return reg


def test_publish_without_server_queues(repo, tmp_path, unreachable_client):
    doc_file = tmp_path / "pol.json"
    doc_file.write_text(json.dumps({"main": {"metric": "val_loss"}}), encoding="utf-8")
    result = invoke("policy", "pack", "publish", str(doc_file))
    assert result.exit_code == 13, result.output


def test_publish_rejects_invalid_json(repo, fake_packs, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    result = invoke("policy", "pack", "publish", str(bad))
    assert result.exit_code == 15, result.output


def test_publish_first_pack_has_no_prev(repo, fake_packs, tmp_path):
    doc = tmp_path / "pol.json"
    doc.write_text(json.dumps({"main": {"metric": "val_loss"}}), encoding="utf-8")
    result = invoke_json("policy", "pack", "publish", str(doc))
    assert result.exit_code == 0, result.output
    env = json.loads(result.output)
    assert env["data"]["prev_id"] is None
    expected_chain = hashlib.sha256(f":{env['data']['object_id']}".encode()).hexdigest()
    assert env["data"]["chain_hash"] == expected_chain


def test_publish_chains_onto_previous(repo, fake_packs, tmp_path):
    doc1 = tmp_path / "pol1.json"
    doc1.write_text(json.dumps({"main": {"metric": "a"}}), encoding="utf-8")
    first = json.loads(invoke_json("policy", "pack", "publish", str(doc1)).output)["data"]

    doc2 = tmp_path / "pol2.json"
    doc2.write_text(json.dumps({"main": {"metric": "b"}}), encoding="utf-8")
    second = json.loads(invoke_json("policy", "pack", "publish", str(doc2)).output)["data"]

    assert second["prev_id"] == first["id"]


def test_show_renders_document(repo, fake_packs, tmp_path):
    doc = tmp_path / "pol.json"
    doc.write_text(json.dumps({"main": {"metric": "val_loss"}}), encoding="utf-8")
    published = json.loads(invoke_json("policy", "pack", "publish", str(doc)).output)["data"]

    result = invoke_json("policy", "pack", "show", published["id"])
    env = json.loads(result.output)
    assert env["data"]["document"]["main"]["metric"] == "val_loss"


def test_log_lists_newest_first(repo, fake_packs, tmp_path):
    for i in range(3):
        doc = tmp_path / f"p{i}.json"
        doc.write_text(json.dumps({"n": i}), encoding="utf-8")
        invoke("policy", "pack", "publish", str(doc))

    result = invoke_json("policy", "pack", "log")
    rows = json.loads(result.output)["data"]["policy_packs"]
    assert len(rows) == 3
    assert rows[0]["created_at"] > rows[1]["created_at"] > rows[2]["created_at"]


def test_verify_confirms_chain_hash(repo, fake_packs, tmp_path):
    doc = tmp_path / "pol.json"
    doc.write_text(json.dumps({"main": {"metric": "val_loss"}}), encoding="utf-8")
    published = json.loads(invoke_json("policy", "pack", "publish", str(doc)).output)["data"]

    result = invoke_json("policy", "pack", "verify", published["id"])
    env = json.loads(result.output)
    assert env["data"]["chain_ok"] is True


def test_verify_detects_broken_chain(repo, fake_packs, tmp_path):
    doc = tmp_path / "pol.json"
    doc.write_text(json.dumps({"main": {"metric": "val_loss"}}), encoding="utf-8")
    published = json.loads(invoke_json("policy", "pack", "publish", str(doc)).output)["data"]
    # Tamper with the stored chain_hash directly, as if the record were corrupted/forged.
    fake_packs.packs[published["id"]]["chain_hash"] = "0" * 64

    result = invoke("policy", "pack", "verify", published["id"])
    assert result.exit_code == 15, result.output

    result_json = invoke_json("policy", "pack", "verify", published["id"])
    assert result_json.exit_code == 15, result_json.output  # v1.3.1 fix: JSON mode used to exit 0
    assert json.loads(result_json.output)["data"]["chain_ok"] is False


def test_verify_unsigned_pack_reports_unsigned(repo, fake_packs, tmp_path):
    doc = tmp_path / "pol.json"
    doc.write_text(json.dumps({"main": {"metric": "val_loss"}}), encoding="utf-8")
    published = json.loads(invoke_json("policy", "pack", "publish", str(doc), "--no-sign").output)["data"]
    assert published["signed"] is False

    result = invoke_json("policy", "pack", "verify", published["id"])
    env = json.loads(result.output)
    assert env["data"]["signature_ok"] is False
    assert env["data"]["reason"] == "unsigned"


def test_publish_gated_by_freeze(repo, fake_packs, tmp_path, monkeypatch):
    monkeypatch.setattr("python.av_cli.cmd_freeze.project_frozen",
                        lambda repo_root: (True, "incident"))
    doc = tmp_path / "pol.json"
    doc.write_text(json.dumps({"main": {}}), encoding="utf-8")
    result = invoke("policy", "pack", "publish", str(doc))
    assert result.exit_code == 18, result.output
