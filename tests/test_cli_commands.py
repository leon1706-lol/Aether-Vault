"""Direct tests for the CLI commands not already covered by tests/test_cli.py:
branch, push, gc, list-meta, config, graph, webui, and the three import-* commands.
"""
import json
import sys
import types

from click.testing import CliRunner

from python.av_cli.main import cli


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


# ---------------------------------------------------------------------------
# av branch
# ---------------------------------------------------------------------------

def test_branch_lists_and_creates(repo):
    result = invoke("branch")
    assert result.exit_code == 0, result.output
    assert "* main" in result.output

    (repo / "f.py").write_text("x = 1")
    invoke("add", "f.py")
    invoke("commit", "-m", "first")

    result = invoke("branch", "feature-x")
    assert result.exit_code == 0, result.output
    assert "Created branch 'feature-x'" in result.output

    result = invoke("branch")
    assert result.exit_code == 0
    assert "* main" in result.output
    assert "feature-x" in result.output


# ---------------------------------------------------------------------------
# av push
# ---------------------------------------------------------------------------

def test_push_with_nothing_pending_is_noop(repo):
    result = invoke("push")
    assert result.exit_code == 0
    assert "nothing pending" in result.output.lower()


def test_push_with_pending_and_unreachable_server_reports_error(repo, monkeypatch):
    # Don't rely on the ambient environment having no real server reachable on the default
    # remote_url — explicitly force "unreachable" so this test is deterministic whether or not
    # a real aether-vault-server happens to be running on localhost:8000 during the test run.
    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.VaultClient, "server_available", lambda self: False)

    (repo / "f.py").write_text("x = 1")
    invoke("add", "f.py")
    invoke("commit", "-m", "first")  # queues .av/pending_push since the server is forced unreachable

    result = invoke("push")
    assert result.exit_code == 0
    assert "not reachable" in result.output.lower()


def test_push_flushes_pending_when_server_reachable(repo, monkeypatch):
    import python.av_cli.main as main_module

    # Force unreachable during the commit so it actually queues .av/pending_push (regardless of
    # whether a real server happens to be reachable in this test run), then force reachable for
    # the push itself.
    monkeypatch.setattr(main_module.VaultClient, "server_available", lambda self: False)
    (repo / "f.py").write_text("x = 1")
    invoke("add", "f.py")
    invoke("commit", "-m", "first")

    monkeypatch.setattr(main_module.VaultClient, "server_available", lambda self: True)
    monkeypatch.setattr(main_module, "flush_pending_push", lambda repo_root, client: [])

    result = invoke("push")
    assert result.exit_code == 0, result.output
    assert "Pushed 1 commit" in result.output


# ---------------------------------------------------------------------------
# upload_commit_objects (commit-latency fix — batch existence check + parallel upload,
# see development/Probleme.md and BENCHMARKS.md #3)
# ---------------------------------------------------------------------------

def test_upload_commit_objects_skips_objects_the_batch_check_reports_found(repo, monkeypatch):
    import python.av_cli.main as main_module

    (repo / "f.py").write_text("x = 1")
    invoke("add", "f.py")
    invoke("commit", "-m", "first")

    idx_entries = json.loads((repo / ".av" / "index").read_text())["entries"]
    tree = {
        rel: {"hash": e["hash"], "size": e["size"], "type": e["type"], "layers": e.get("layers", [])}
        for rel, e in idx_entries.items()
    }
    tracked_hash = next(iter(tree.values()))["hash"]

    calls = {"batch_check": [], "uploaded": []}

    class FakeClient:
        def batch_check_objects(self, hashes):
            calls["batch_check"].append(set(hashes))
            return {tracked_hash}  # server already has it

        def upload_object(self, path, h, known_missing=False):
            calls["uploaded"].append((h, known_missing))
            return True

    main_module.upload_commit_objects(repo, FakeClient(), tree)

    assert calls["batch_check"] == [{tracked_hash}]
    assert calls["uploaded"] == []  # nothing missing -> nothing uploaded


def test_upload_commit_objects_uploads_only_missing_hashes(repo, monkeypatch):
    import python.av_cli.main as main_module

    (repo / "a.py").write_text("a = 1")
    (repo / "b.py").write_text("b = 2")
    invoke("add", "a.py", "b.py")
    invoke("commit", "-m", "first")

    idx_entries = json.loads((repo / ".av" / "index").read_text())["entries"]
    tree = {
        rel: {"hash": e["hash"], "size": e["size"], "type": e["type"], "layers": e.get("layers", [])}
        for rel, e in idx_entries.items()
    }
    all_hashes = {info["hash"] for info in tree.values()}
    missing_hash = next(iter(all_hashes))

    calls = {"uploaded": []}

    class FakeClient:
        def batch_check_objects(self, hashes):
            assert set(hashes) == all_hashes
            return all_hashes - {missing_hash}

        def upload_object(self, path, h, known_missing=False):
            calls["uploaded"].append((h, known_missing))
            return True

    main_module.upload_commit_objects(repo, FakeClient(), tree)

    assert calls["uploaded"] == [(missing_hash, True)]


# ---------------------------------------------------------------------------
# av gc
# ---------------------------------------------------------------------------

def test_gc_reports_error_when_server_unreachable(repo, monkeypatch):
    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.VaultClient, "server_available", lambda self: False)

    result = invoke("gc")
    assert result.exit_code == 0
    assert "not reachable" in result.output.lower()


def test_gc_reports_success_with_mocked_client(repo, monkeypatch):
    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.VaultClient, "server_available", lambda self: True)
    monkeypatch.setattr(
        main_module.VaultClient,
        "run_gc",
        lambda self: {"alive_objects": 3, "deleted_objects": 1, "reused_trees": 2},
    )

    result = invoke("gc")
    assert result.exit_code == 0, result.output
    assert "Garbage collection complete" in result.output
    assert "Alive objects : 3" in result.output


# ---------------------------------------------------------------------------
# av list-meta
# ---------------------------------------------------------------------------

def test_list_meta_empty_repo_shows_none(repo):
    result = invoke("list-meta")
    assert result.exit_code == 0, result.output
    assert "(none)" in result.output


def test_list_meta_displays_tags_and_metrics(repo):
    (repo / "f.py").write_text("x = 1")
    invoke("add", "f.py")
    invoke("commit", "-m", "first", "--tag", "v1", "--metric", "sharpe=2.5")

    result = invoke("list-meta")
    assert result.exit_code == 0, result.output
    assert "v1" in result.output
    assert "sharpe" in result.output


# ---------------------------------------------------------------------------
# av config
# ---------------------------------------------------------------------------

def test_config_prints_current_values(repo):
    result = invoke("config")
    assert result.exit_code == 0, result.output
    assert "LFS threshold" in result.output
    assert "50 MB" in result.output


def test_config_updates_persist(repo):
    result = invoke("config", "--remote-url", "http://example.com:9000", "--name", "my-project")
    assert result.exit_code == 0, result.output

    cfg = json.loads((repo / ".av" / "config").read_text())
    assert cfg["remote_url"] == "http://example.com:9000"
    assert cfg["project_name"] == "my-project"


# ---------------------------------------------------------------------------
# av graph
# ---------------------------------------------------------------------------

def test_graph_update_generates_vault_without_opening_browser(repo):
    result = invoke("graph", "--update")
    assert result.exit_code == 0, result.output
    assert (repo / "Aether-Graph").is_dir()
    assert "Attempted to launch Obsidian" not in result.output


# ---------------------------------------------------------------------------
# av webui
# ---------------------------------------------------------------------------

def test_webui_reports_docker_not_running(repo, monkeypatch):
    import python.av_cli.docker_runtime as docker_runtime_module

    def fake_run(args, **kwargs):
        return type("FakeCompleted", (), {"returncode": 1})()

    monkeypatch.setattr(docker_runtime_module.subprocess, "run", fake_run, raising=False)

    result = invoke("webui")
    assert result.exit_code == 0, result.output
    assert "Docker is not running" in result.output


# ---------------------------------------------------------------------------
# av import-lightning / import-transformers / import-mlflow
#
# The real av_plugins.lightning/transformers modules raise ImportError at import time when
# the optional extra isn't installed (not installed in this dev environment), so they can't be
# imported first and then monkeypatched in the usual way. Instead, inject a fake module
# directly into sys.modules before invoking the CLI — `from av_plugins.lightning import
# import_checkpoint` inside the command function then resolves to the fake module without ever
# executing the real (dependency-requiring) file.
# ---------------------------------------------------------------------------

def test_import_lightning_cli_wraps_plugin_function(repo, monkeypatch, tmp_path):
    calls = {}

    def fake_import_checkpoint(checkpoint_path, repo_root=None, tag=None):
        calls["args"] = (checkpoint_path, repo_root, tag)

    fake_module = types.ModuleType("av_plugins.lightning")
    fake_module.import_checkpoint = fake_import_checkpoint
    monkeypatch.setitem(sys.modules, "av_plugins.lightning", fake_module)

    ckpt = tmp_path / "epoch=1.ckpt"
    ckpt.write_bytes(b"fake checkpoint")

    result = invoke("import-lightning", str(ckpt), "--tag", "backfill")
    assert result.exit_code == 0, result.output
    assert calls["args"][0] == str(ckpt)
    assert calls["args"][2] == "backfill"
    assert "Imported Lightning checkpoint" in result.output


def test_import_transformers_cli_wraps_plugin_function(repo, monkeypatch, tmp_path):
    calls = {}

    def fake_import_checkpoint(checkpoint_dir, repo_root=None, tag=None):
        calls["args"] = (checkpoint_dir, repo_root, tag)

    fake_module = types.ModuleType("av_plugins.transformers")
    fake_module.import_checkpoint = fake_import_checkpoint
    monkeypatch.setitem(sys.modules, "av_plugins.transformers", fake_module)

    ckpt_dir = tmp_path / "checkpoint-1000"
    ckpt_dir.mkdir()

    result = invoke("import-transformers", str(ckpt_dir), "--tag", "backfill")
    assert result.exit_code == 0, result.output
    assert calls["args"][0] == str(ckpt_dir)
    assert calls["args"][2] == "backfill"
    assert "Imported Transformers checkpoint" in result.output


def test_import_mlflow_cli_wraps_plugin_function(repo, monkeypatch):
    calls = {}

    def fake_import_run(run_id, repo_root=None, tracking_uri=None, tag=None):
        calls["args"] = (run_id, repo_root, tracking_uri, tag)

    fake_module = types.ModuleType("av_plugins.mlflow")
    fake_module.import_run = fake_import_run
    monkeypatch.setitem(sys.modules, "av_plugins.mlflow", fake_module)

    result = invoke("import-mlflow", "run-123", "--tracking-uri", "sqlite:///x.db", "--tag", "backfill")
    assert result.exit_code == 0, result.output
    assert calls["args"][0] == "run-123"
    assert calls["args"][2] == "sqlite:///x.db"
    assert calls["args"][3] == "backfill"
    assert "Imported MLflow run" in result.output
