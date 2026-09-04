"""av canary — capability canary checks (v1.3.1).

The check-EVALUATION path (register a suite, evaluate it against HEAD's metrics) is
local-only and tested through the real CLI here. The result-REPORTING path
(POST /api/canary-results) degrades gracefully offline (tested here too) and its live
round trip belongs in tests/test_server.py.
"""
import json

from click.testing import CliRunner

from python.av_cli.main import cli


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def invoke_json(*args):
    return CliRunner().invoke(cli, ["--output", "json", *args])


def _write_suite(repo, name="core", checks=None):
    suite = {"checks": checks or [
        {"name": "val_loss_not_worse", "metric": "val_loss", "op": "<=", "threshold": 0.6},
    ]}
    p = repo / f"{name}.json"
    p.write_text(json.dumps(suite), encoding="utf-8")
    return p


def test_register_and_list(repo):
    suite_file = _write_suite(repo)
    result = invoke_json("canary", "register", "core", str(suite_file))
    assert result.exit_code == 0, result.output
    env = json.loads(result.output)
    assert env["data"]["checks"] == 1

    listed = invoke_json("canary", "list")
    assert listed.exit_code == 0
    assert "core" in json.loads(listed.output)["data"]["suites"]


def test_register_rejects_malformed_suite(repo):
    bad = repo / "bad.json"
    bad.write_text(json.dumps({"not_checks": []}), encoding="utf-8")
    result = invoke("canary", "register", "bad", str(bad))
    assert result.exit_code == 15, result.output


def test_register_unknown_name_errors_cleanly(repo):
    result = invoke_json("canary", "run", "nope")
    assert result.exit_code == 15, result.output
    env = json.loads(result.output)
    assert env["error"]["code"] == "validation"


def test_run_passes_when_metric_satisfies_threshold(repo):
    suite_file = _write_suite(repo)
    invoke("canary", "register", "core", str(suite_file))

    (repo / "m.txt").write_text("v1")
    invoke("add", "m.txt")
    invoke("commit", "-m", "good", "--metric", "val_loss=0.5", "--no-upload")

    result = invoke_json("canary", "run", "core")
    assert result.exit_code == 0, result.output
    env = json.loads(result.output)
    assert env["data"]["passed"] is True
    assert env["data"]["checks"][0]["passed"] is True


def test_run_fails_when_metric_violates_threshold(repo):
    suite_file = _write_suite(repo)
    invoke("canary", "register", "core", str(suite_file))

    (repo / "m.txt").write_text("v1")
    invoke("add", "m.txt")
    invoke("commit", "-m", "bad", "--metric", "val_loss=0.9", "--no-upload")

    result = invoke("canary", "run", "core")
    assert result.exit_code == 15, result.output

    result_json = invoke_json("canary", "run", "core")
    assert result_json.exit_code == 15, result_json.output  # v1.3.1 fix: JSON mode used to exit 0
    env = json.loads(result_json.output)
    assert env["data"]["passed"] is False
    assert env["data"]["checks"][0]["passed"] is False


def test_run_fails_when_metric_absent_from_head(repo):
    suite_file = _write_suite(repo)
    invoke("canary", "register", "core", str(suite_file))

    (repo / "m.txt").write_text("v1")
    invoke("add", "m.txt")
    invoke("commit", "-m", "no metrics", "--no-upload")

    result = invoke_json("canary", "run", "core")
    env = json.loads(result.output)
    assert env["data"]["passed"] is False
    assert env["data"]["checks"][0]["value"] is None


def test_run_with_multiple_checks_requires_all_to_pass(repo):
    suite_file = _write_suite(repo, checks=[
        {"name": "val_loss_ok", "metric": "val_loss", "op": "<=", "threshold": 0.6},
        {"name": "acc_ok", "metric": "acc", "op": ">=", "threshold": 0.9},
    ])
    invoke("canary", "register", "core", str(suite_file))

    (repo / "m.txt").write_text("v1")
    invoke("add", "m.txt")
    invoke("commit", "-m", "partial", "--metric", "val_loss=0.5", "--no-upload")  # acc missing

    result = invoke_json("canary", "run", "core")
    env = json.loads(result.output)
    assert env["data"]["passed"] is False
    passed_by_name = {c["name"]: c["passed"] for c in env["data"]["checks"]}
    assert passed_by_name["val_loss_ok"] is True
    assert passed_by_name["acc_ok"] is False


def test_run_offline_still_evaluates_but_does_not_report(repo):
    """No server reachable in the test sandbox — the LOCAL evaluation must still work,
    matching offline-resilience expectations for a read/evaluate-only operation."""
    suite_file = _write_suite(repo)
    invoke("canary", "register", "core", str(suite_file))
    (repo / "m.txt").write_text("v1")
    invoke("add", "m.txt")
    invoke("commit", "-m", "good", "--metric", "val_loss=0.1", "--no-upload")

    result = invoke_json("canary", "run", "core", "--improver", "fake-improver-id")
    env = json.loads(result.output)
    assert env["data"]["passed"] is True
    assert env["data"]["reported"] is False  # unreachable server, degrades gracefully


def test_status_with_no_improver_pointer_and_no_arg_fails_validation(repo):
    result = invoke("canary", "status")
    assert result.exit_code == 15, result.output
