"""Tests for `av support-bundle` (v1.3.2, WP-34). Redaction is the load-bearing
assertion here — everything else is a thin structural check."""
import json

from click.testing import CliRunner

from python.av_cli import cmd_support
from python.av_cli.main import cli


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def test_redact_masks_credential_shaped_keys():
    raw = {
        "remote_url": "http://localhost:8000",
        "remote_api_token": "super-secret-value-12345",
        "nested": {"api_key": "another-secret", "count": 3},
        "list_field": [{"password": "yet-another-secret"}, "plain-string"],
    }
    redacted = cmd_support._redact(raw)
    assert redacted["remote_url"] == "http://localhost:8000"
    assert redacted["remote_api_token"] == "***REDACTED***"
    assert redacted["nested"]["api_key"] == "***REDACTED***"
    assert redacted["nested"]["count"] == 3
    assert redacted["list_field"][0]["password"] == "***REDACTED***"
    assert redacted["list_field"][1] == "plain-string"


def test_redact_leaves_falsy_credential_values_alone():
    # An empty/None token shouldn't become the literal string "***REDACTED***" -- that
    # would be MORE misleading than leaving it, implying a token exists when none does.
    redacted = cmd_support._redact({"remote_api_token": None})
    assert redacted["remote_api_token"] is None


def test_support_bundle_writes_a_redacted_bundle(repo, tmp_path, monkeypatch):
    config_path = repo / ".av" / "config"
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    cfg["remote_api_token"] = "the-raw-secret-token-xyz"
    config_path.write_text(json.dumps(cfg), encoding="utf-8")

    out_dir = tmp_path / "bundle-out"
    result = invoke("support-bundle", str(out_dir))
    assert result.exit_code == 0, result.output

    bundle_path = out_dir / "bundle.json"
    assert bundle_path.is_file()
    raw_text = bundle_path.read_text(encoding="utf-8")
    assert "the-raw-secret-token-xyz" not in raw_text

    bundle = json.loads(raw_text)
    assert bundle["repo_config"]["remote_api_token"] == "***REDACTED***"
    assert "cli_version" in bundle
    assert "health" in bundle
    assert "ready" in bundle


def test_support_bundle_json_mode_envelope(repo, tmp_path):
    out_dir = tmp_path / "bundle-json"
    result = invoke("--output", "json", "support-bundle", str(out_dir))
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["output_dir"] == str(out_dir)
