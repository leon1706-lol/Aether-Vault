"""V1.2.2 unit-level tests: .avh flow-through, schema-file validation, audit CLI params.

Covers the plan's Part-3 "Unit" row:
- dedup_efficiency flows from semdiff into .avh.semantic_summary (gap 2)
- the shipped avh-2.0 JSON-Schema artifact validates a golden document and rejects a
  broken one — with jsonschema if available; the schema FILE is parsed either way so a
  malformed artifact fails here regardless of extras
- `av audit list` builds its query params correctly (client-side contract)
"""
import importlib.util
import json
from pathlib import Path

import pytest

from click.testing import CliRunner

from python.av_cli import cmd_audit
from python.av_cli.attributes import load_attributes  # noqa: F401 (parity with core use)
from python.av_cli.handoff import build_handoff_dict, validate_handoff
from python.av_cli.semdiff import diff_trees
from python.av_cli.main import cli

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "python" / "av_cli" / "schemas" / "avh-2.0.schema.json"


def _init_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cli, ["init", "--mode", "local", "--yes", "--no-repl"],
                             standalone_mode=False)
    assert res.exit_code == 0, res.output


def _commit_tree(repo_root, tree: dict, message: str):
    """Commits a synthetic flat tree via the real single writer. tree entries are full
    index-entry dicts."""
    from python.av_cli.core import commit_staged
    from python.av_cli.index import Index

    idx = Index(repo_root)
    idx.entries.update(tree)
    idx.save()
    return commit_staged(repo_root, message)


def _entry(file_hash, chunks):
    return {
        "hash": file_hash, "size": sum(c["size"] for c in chunks), "mtime_ns": 0,
        "type": "artifact", "staged": True, "pointer": None,
        "chunks": list(chunks),
    }


# ---------------------------------------------------------------------------
# dedup_efficiency → .avh.semantic_summary
# ---------------------------------------------------------------------------

def test_dedup_efficiency_flows_into_avh_semantic_summary(tmp_path, monkeypatch):
    """Parent chunks {A,B} → child chunks {A,fresh}: efficiency must be 1/2 in BOTH the
    raw engine output and whatever lands in .avh.semantic_summary."""
    _init_repo(tmp_path, monkeypatch)

    chunk_a = {"hash": "a" * 64, "size": 10, "offset": 0}
    chunk_b = {"hash": "b" * 64, "size": 10, "offset": 10}
    chunk_new = {"hash": "f" * 64, "size": 10, "offset": 20}

    parent_hash = _commit_tree(
        tmp_path, {"m.pt": _entry("p" * 64, [chunk_a, chunk_b])}, "parent")

    # Child reuses A, replaces B with fresh:
    child_hash = _commit_tree(
        tmp_path,
        {"m.pt": dict(_entry("c" * 64, [chunk_a, chunk_new]), staged=True)},
        "child",
    )

    parent_tree = json.loads(
        (tmp_path / ".av" / "commits" / f"{parent_hash}.json").read_text())["tree"]
    child_commit = json.loads(
        (tmp_path / ".av" / "commits" / f"{child_hash}.json").read_text())

    engine = diff_trees(parent_tree, child_commit["tree"])
    assert engine["chunks"]["reused"] == 1
    assert engine["chunks"]["new"] == 1
    assert engine["chunks"]["dedup_efficiency"] == 0.5

    avh = build_handoff_dict(tmp_path, None)
    assert avh["semantic_summary"] is not None
    assert avh["semantic_summary"]["chunks"] == engine["chunks"], \
        ".avh.semantic_summary lost the dedup_efficiency flow-through"


# ---------------------------------------------------------------------------
# Schema-file validation path
# ---------------------------------------------------------------------------

def test_schema_artifact_parses_and_matches_contract():
    assert _SCHEMA_PATH.exists(), "avh-2.0.schema.json missing from package data"
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$id"].endswith("avh-2.0.json")
    for key in ("$schema", "avh_version", "generated_at", "current_branch",
                "lineage", "context_memory"):
        assert key in schema["required"], f"schema lost required key {key}"


@pytest.mark.skipif(importlib.util.find_spec("jsonschema") is None,
                    reason="jsonschema not installed")
def test_golden_document_validates_against_schema_file(tmp_path, monkeypatch):
    import jsonschema

    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    doc = build_handoff_dict(_golden_repo(tmp_path, monkeypatch), "hand me the loop")
    jsonschema.validate(doc, schema)  # raises on any violation

    broken = dict(doc)
    broken["avh_version"] = "1.0"  # schema pattern ^2\.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(broken, schema)


def test_structural_validator_agrees_on_golden_and_broken(tmp_path, monkeypatch):
    repo = _golden_repo(tmp_path, monkeypatch)
    doc = build_handoff_dict(repo, None)
    assert validate_handoff(doc) == []

    broken = dict(doc)
    broken.pop("lineage")
    problems = validate_handoff(broken)
    assert any("lineage" in p for p in problems)


def _golden_repo(tmp_path, monkeypatch):
    _init_repo(tmp_path, monkeypatch)
    return tmp_path


# ---------------------------------------------------------------------------
# av audit list client-side param building
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload, status=200, content: bytes | None = None):
        self._payload, self.status_code = payload, status
        # export/prune (v1.2.5) read .content/.text directly rather than .json() for the
        # success/error paths respectively — default content mirrors the JSON payload so
        # tests that don't care about the raw bytes still get something sane.
        self.content = content if content is not None else json.dumps(payload).encode()
        self.text = self.content.decode("utf-8", errors="replace")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responder, delete_responder=None):
        self._responder = responder
        self._delete_responder = delete_responder

    def get(self, url, params=None, timeout=None):
        return self._responder(url, params)

    def delete(self, url, params=None, timeout=None):
        if self._delete_responder is None:
            return _FakeResponse({"deleted": 0})
        return self._delete_responder(url, params)


class _FakeClient:
    def __init__(self, responder=None, delete_responder=None):
        self.server_url = "http://fake"
        self.calls = []
        self.delete_calls = []
        self.session = _FakeSession(self._record(responder), self._record_delete(delete_responder))

    def _record(self, responder):
        def _get(url, params=None, timeout=None):
            self.calls.append((url, params))
            if responder is None:
                return _FakeResponse({"entries": [], "total": 0})
            return responder(url, params)
        return _get

    def _record_delete(self, responder):
        def _delete(url, params=None):
            self.delete_calls.append((url, params))
            if responder is None:
                return _FakeResponse({"deleted": 0})
            return responder(url, params)
        return _delete


def test_audit_list_builds_filters_and_defaults(monkeypatch, tmp_path):
    fake = _FakeClient()
    monkeypatch.setattr(cmd_audit, "_client", lambda repo_root: fake)
    _init_repo(tmp_path, monkeypatch)

    res = CliRunner().invoke(cli, ["audit", "list"], standalone_mode=False)
    assert res.exit_code == 0, res.output
    url, params = fake.calls[-1]
    assert url.endswith("/api/admin/audit")
    assert params["limit"] == 50 and params["offset"] == 0
    assert "action" not in params and "project_id" not in params


def test_audit_list_passes_every_filter_through(monkeypatch, tmp_path):
    fake = _FakeClient()
    monkeypatch.setattr(cmd_audit, "_client", lambda repo_root: fake)
    _init_repo(tmp_path, monkeypatch)

    res = CliRunner().invoke(cli, [
        "audit", "list", "--action", "commit.push", "--project", "proj-1",
        "--since", "2026-08-01T00:00:00", "--until", "2026-08-26T23:59:59",
        "--limit", "10", "--offset", "20",
    ], standalone_mode=False)
    assert res.exit_code == 0, res.output
    _, params = fake.calls[-1]
    assert params == {
        "limit": 10, "offset": 20, "action": "commit.push",
        "project_id": "proj-1",
        "since": "2026-08-01T00:00:00", "until": "2026-08-26T23:59:59",
    }


def test_audit_list_json_envelope_shape(monkeypatch, tmp_path):
    entries = [{
        "id": 7, "ts": "2026-08-26T12:00:00", "username": "alice",
        "action": "commit.push", "project_id": "p1", "details": {"hash": "ab"},
        "status_code": 201,
    }]
    fake = _FakeClient(responder=lambda url, params: _FakeResponse(
        {"entries": entries, "total": 1}))
    monkeypatch.setattr(cmd_audit, "_client", lambda repo_root: fake)
    _init_repo(tmp_path, monkeypatch)

    res = CliRunner().invoke(cli, ["--output", "json", "audit", "list"],
                             standalone_mode=False)
    envelope = json.loads(res.output)
    assert envelope["ok"] is True
    assert envelope["data"]["entries"][0]["status_code"] == 201
    assert envelope["meta"]["command"] == "audit list"


# ---------------------------------------------------------------------------
# v1.2.5: richer audit filters, cursor pagination, export, prune CLI
# ---------------------------------------------------------------------------

def test_audit_list_passes_new_filters_through(monkeypatch, tmp_path):
    fake = _FakeClient()
    monkeypatch.setattr(cmd_audit, "_client", lambda repo_root: fake)
    _init_repo(tmp_path, monkeypatch)

    res = CliRunner().invoke(cli, [
        "audit", "list", "--action-prefix", "commit.", "--username", "alice",
        "--status-code", "409", "--outcome", "error",
    ], standalone_mode=False)
    assert res.exit_code == 0, res.output
    _, params = fake.calls[-1]
    assert params["action_prefix"] == "commit."
    assert params["username"] == "alice"
    assert params["status_code"] == 409
    assert params["outcome"] == "error"


def test_audit_list_cursor_and_offset_are_mutually_exclusive_client_side(monkeypatch, tmp_path):
    """--cursor replaces --offset in the outgoing request rather than sending both —
    the server itself also 422s a request carrying both, but the CLI shouldn't even try."""
    fake = _FakeClient()
    monkeypatch.setattr(cmd_audit, "_client", lambda repo_root: fake)
    _init_repo(tmp_path, monkeypatch)

    res = CliRunner().invoke(cli, ["audit", "list", "--cursor", "abc123"], standalone_mode=False)
    assert res.exit_code == 0, res.output
    _, params = fake.calls[-1]
    assert params.get("cursor") == "abc123"
    assert "offset" not in params


def test_audit_export_builds_filters_and_format(monkeypatch, tmp_path):
    fake = _FakeClient(responder=lambda url, params: _FakeResponse(
        {}, content=b"id,ts\n1,2026-01-01\n"))
    monkeypatch.setattr(cmd_audit, "_client", lambda repo_root: fake)
    _init_repo(tmp_path, monkeypatch)

    res = CliRunner().invoke(cli, [
        "audit", "export", "--format", "csv", "--action", "commit.push",
    ], standalone_mode=False)
    assert res.exit_code == 0, res.output
    url, params = fake.calls[-1]
    assert url.endswith("/api/admin/audit/export")
    assert params["format"] == "csv"
    assert params["action"] == "commit.push"
    assert "id,ts" in res.output


def test_audit_export_writes_to_file(monkeypatch, tmp_path):
    fake = _FakeClient(responder=lambda url, params: _FakeResponse(
        {}, content=b'{"id": 1}\n'))
    monkeypatch.setattr(cmd_audit, "_client", lambda repo_root: fake)
    _init_repo(tmp_path, monkeypatch)
    out_file = tmp_path / "audit.jsonl"

    res = CliRunner().invoke(cli, ["audit", "export", "--out", str(out_file)],
                             standalone_mode=False)
    assert res.exit_code == 0, res.output
    assert out_file.read_bytes() == b'{"id": 1}\n'


def test_audit_prune_confirms_by_default_and_skips_with_yes(monkeypatch, tmp_path):
    fake = _FakeClient(delete_responder=lambda url, params: _FakeResponse({"deleted": 3}))
    monkeypatch.setattr(cmd_audit, "_client", lambda repo_root: fake)
    _init_repo(tmp_path, monkeypatch)

    # Declining the prompt prunes nothing.
    declined = CliRunner().invoke(cli, ["audit", "prune", "--before-days", "30"],
                                  input="n\n", standalone_mode=False)
    assert declined.exit_code == 0, declined.output
    assert not fake.delete_calls

    # --yes skips the prompt outright.
    res = CliRunner().invoke(cli, ["audit", "prune", "--before-days", "30", "--yes"],
                             standalone_mode=False)
    assert res.exit_code == 0, res.output
    assert "Pruned 3" in res.output
    _, params = fake.delete_calls[-1]
    assert params == {"before_days": 30}


def test_audit_prune_json_mode_skips_prompt(monkeypatch, tmp_path):
    fake = _FakeClient(delete_responder=lambda url, params: _FakeResponse({"deleted": 0}))
    monkeypatch.setattr(cmd_audit, "_client", lambda repo_root: fake)
    _init_repo(tmp_path, monkeypatch)

    res = CliRunner().invoke(cli, ["--output", "json", "audit", "prune"], standalone_mode=False)
    assert res.exit_code == 0, res.output
    envelope = json.loads(res.output)
    assert envelope["data"]["deleted"] == 0
    assert fake.delete_calls  # ran without any interactive prompt


def test_audit_prune_dry_run_deletes_nothing_and_skips_the_prompt(monkeypatch, tmp_path):
    # v1.3.0: --dry-run reports would_delete without a confirm prompt (nothing to
    # confirm — it can't destroy anything) and without deleting anything server-side.
    fake = _FakeClient(delete_responder=lambda url, params: _FakeResponse(
        {"deleted": 0, "would_delete": 7, "dry_run": True}))
    monkeypatch.setattr(cmd_audit, "_client", lambda repo_root: fake)
    _init_repo(tmp_path, monkeypatch)

    res = CliRunner().invoke(cli, ["audit", "prune", "--before-days", "30", "--dry-run"],
                             standalone_mode=False)
    assert res.exit_code == 0, res.output
    assert "Would delete 7" in res.output
    _, params = fake.delete_calls[-1]
    assert params == {"before_days": 30, "dry_run": "true"}


def test_audit_prune_dry_run_json_mode(monkeypatch, tmp_path):
    fake = _FakeClient(delete_responder=lambda url, params: _FakeResponse(
        {"deleted": 0, "would_delete": 12, "dry_run": True}))
    monkeypatch.setattr(cmd_audit, "_client", lambda repo_root: fake)
    _init_repo(tmp_path, monkeypatch)

    res = CliRunner().invoke(cli, ["--output", "json", "audit", "prune", "--dry-run"],
                             standalone_mode=False)
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)["data"]
    assert data == {"deleted": 0, "would_delete": 12, "dry_run": True, "before_days": None}
