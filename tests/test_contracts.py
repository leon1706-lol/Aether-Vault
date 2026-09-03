"""Contract-freeze tests (v1.3.0, todo.md item 27): drives the REAL CLI and validates its
live output against the published schemas in python/av_cli/schemas/*.schema.json, using
core.py::load_contract_schema() — the same loader real external validators would use once
these ship in the wheel. Stack-free: everything here runs against a temp `repo` fixture,
no live Postgres/Redis/registry needed. The run/event/webhook-payload schemas are instead
validated against the live server in tests/test_server.py (reachability-gated there, same
as every other live-service assertion in that file) — this file only covers the schemas a
plain CLI invocation can produce on its own: envelope, semdiff, avh.

Requires the `dev` extra's jsonschema (pyproject.toml) — skips cleanly without it, same
philosophy as test_v122.py's existing golden-document check.
"""
import importlib.util
import json

import pytest
from click.testing import CliRunner

from python.av_cli.core import load_contract_schema
from python.av_cli.main import cli

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("jsonschema") is None, reason="jsonschema not installed (dev extra)"
)


def _invoke_json(*args):
    result = CliRunner().invoke(cli, ["--output", "json", *args])
    assert result.exit_code in (0, 10, 11, 12, 13, 14, 15, 16), result.output
    return json.loads(result.output)


def _validate(instance: dict, schema_name: str) -> None:
    import jsonschema

    schema = load_contract_schema(schema_name)
    jsonschema.validate(instance, schema)


class TestLoadContractSchema:
    def test_loads_every_published_schema(self):
        for name in ("envelope-1.0", "event-1.0", "run-1.0", "webhook-payload-1.0",
                     "semdiff-1.0", "avh-2.0"):
            schema = load_contract_schema(name)
            assert schema["$id"].startswith("https://aether-vault.dev/schemas/")

    def test_unknown_name_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_contract_schema("does-not-exist-9.9")


class TestEnvelopeSchema:
    def test_ok_status_envelope_matches_schema(self, repo):
        env = _invoke_json("status")
        _validate(env, "envelope-1.0")
        assert env["ok"] is True
        assert env["error"] is None

    def test_error_envelope_matches_schema(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        env = _invoke_json("status")
        _validate(env, "envelope-1.0")
        assert env["ok"] is False
        assert env["error"]["code"] == "not_a_repo"


class TestSemdiffSchema:
    def test_diff_output_matches_schema(self, repo):
        (repo / "model.safetensors").write_bytes(b"weights-v1")
        CliRunner().invoke(cli, ["add", "model.safetensors"])
        CliRunner().invoke(cli, ["commit", "-m", "v1"])
        (repo / "model.safetensors").write_bytes(b"weights-v2-longer")
        CliRunner().invoke(cli, ["add", "model.safetensors"])
        CliRunner().invoke(cli, ["commit", "-m", "v2"])

        env = _invoke_json("diff")
        _validate(env, "envelope-1.0")
        _validate(env["data"], "semdiff-1.0")
        assert env["data"]["files"]["changed"] or env["data"]["files"]["added"]

    def test_empty_repo_diff_still_matches_schema(self, repo):
        # No commits at all yet: base/target are null, but files/models/chunks/datasets/
        # totals must still be the always-present shape diff_trees() guarantees.
        env = _invoke_json("diff")
        _validate(env["data"], "semdiff-1.0")


class TestAvhSchema:
    def test_handoff_document_matches_schema(self, repo):
        (repo / "w.pt").write_bytes(b"weights")
        CliRunner().invoke(cli, ["add", "w.pt"])
        CliRunner().invoke(cli, ["commit", "-m", "first"])
        CliRunner().invoke(cli, ["handoff"])

        doc = json.loads((repo / "handoff.avh").read_text(encoding="utf-8"))
        _validate(doc, "avh-2.0")
        # v1.3.0 tightening: semantic_summary, when present, always carries the full
        # chunks sub-shape — this is the behavior the schema now requires (was optional).
        if doc.get("semantic_summary") is not None:
            assert set(("reused", "new", "status")) <= set(doc["semantic_summary"]["chunks"])
