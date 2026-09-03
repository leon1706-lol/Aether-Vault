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
    import python.av_cli.cmd_history as cmd_history
    import python.av_cli.main as main_module

    # Force unreachable during the commit so it actually queues .av/pending_push (regardless of
    # whether a real server happens to be reachable in this test run), then force reachable for
    # the push itself.
    monkeypatch.setattr(main_module.VaultClient, "server_available", lambda self: False)
    (repo / "f.py").write_text("x = 1")
    invoke("add", "f.py")
    invoke("commit", "-m", "first")

    monkeypatch.setattr(main_module.VaultClient, "server_available", lambda self: True)
    # flush_pending_push moved to core in the Point-13 split; `push` resolves it from its
    # own command module's namespace, so the patch target follows the new owner.
    monkeypatch.setattr(cmd_history, "flush_pending_push", lambda repo_root, client: [])

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

    result = main_module.upload_commit_objects(repo, FakeClient(), tree)

    assert calls["uploaded"] == [(missing_hash, True)]
    assert result is True


def test_upload_commit_objects_returns_false_when_any_upload_fails(repo, monkeypatch):
    # v1.3.0 (Probleme #126): found live on a real Docker/GHA registry with an unwritable
    # data dir — the server's storage write failed (a clean 500, upload_object() correctly
    # returned False for it), but this function's return value was discarded by every
    # caller, so a commit landed referencing an object that was never actually stored.
    # DBTree.object_hash is deliberately NOT a real DB foreign key (see its own comment in
    # av_server/models.py — layer-split artifacts never get a whole-file object row), so
    # this return value is the ONLY signal a real upload failure ever produces; callers
    # MUST treat False as a reason to queue instead of pushing the commit.
    import python.av_cli.main as main_module

    (repo / "c.py").write_text("c = 1")
    invoke("add", "c.py")
    invoke("commit", "-m", "first")

    idx_entries = json.loads((repo / ".av" / "index").read_text())["entries"]
    tree = {
        rel: {"hash": e["hash"], "size": e["size"], "type": e["type"], "layers": e.get("layers", [])}
        for rel, e in idx_entries.items()
    }
    all_hashes = {info["hash"] for info in tree.values()}

    class FakeClient:
        def batch_check_objects(self, hashes):
            return set()  # nothing on the server yet -> every hash is missing

        def upload_object(self, path, h, known_missing=False):
            return False  # simulates a real storage write failure (e.g. unwritable disk)

    result = main_module.upload_commit_objects(repo, FakeClient(), tree)

    assert result is False


def test_commit_queues_instead_of_pushing_when_an_object_upload_fails(repo, monkeypatch):
    # End-to-end version of the unit test above: drives the real `av commit` command and
    # asserts the commit lands locally but gets QUEUED for retry, never pushed, when an
    # object upload fails — the exact scenario a real unwritable/full registry disk
    # produces (development/Probleme.md #126; caught live by scripts/e2e_scenario.sh's
    # Phase M on a real GHA Linux runner).
    from python.av_cli.client import VaultClient

    monkeypatch.setattr(VaultClient, "server_available", lambda self: True)
    monkeypatch.setattr(VaultClient, "batch_check_objects", lambda self, hashes: set())
    monkeypatch.setattr(VaultClient, "upload_object",
                        lambda self, path, h, known_missing=False: False)
    # push_commit must never even be attempted once the object upload has already failed —
    # fail loudly if it is, rather than silently landing metadata for unstored bytes.
    monkeypatch.setattr(
        VaultClient, "push_commit",
        lambda self, commit_data: (_ for _ in ()).throw(
            AssertionError("push_commit() must not be called after a failed object upload")
        ),
    )

    (repo / "train.py").write_text("print('hi')")
    invoke("add", "train.py")
    result = invoke("commit", "-m", "should queue, not push unstored content")
    assert result.exit_code == 0, result.output
    assert "queued for retry" in result.output.lower()

    commit_hash = (repo / ".av" / "refs" / "heads" / "main").read_text().strip()
    pending = json.loads((repo / ".av" / "pending_push").read_text())
    assert any(p["commit_hash"] == commit_hash for p in pending)


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
# av update --docker
# ---------------------------------------------------------------------------

def test_update_docker_dev_checkout_noops(repo, monkeypatch):
    import python.av_cli.docker_runtime as docker_runtime_module

    monkeypatch.setattr(
        docker_runtime_module, "check_for_docker_update",
        lambda source_root: docker_runtime_module.DockerUpdateResult(
            checked=False, message="Running from a source checkout — use `git pull` + `av webui --rebuild` instead.",
        ),
    )
    result = invoke("update", "--docker")
    assert result.exit_code == 0, result.output
    assert "source checkout" in result.output


def test_update_docker_pulls_and_restarts_with_yes(repo, monkeypatch):
    import python.av_cli.docker_runtime as docker_runtime_module

    monkeypatch.setattr(
        docker_runtime_module, "check_for_docker_update",
        lambda source_root: docker_runtime_module.DockerUpdateResult(
            checked=True, updated=True, message="A newer Docker image was pulled.",
            old_image_ids=["old-server-id", "old-webui-id"],
        ),
    )
    monkeypatch.setattr(
        docker_runtime_module, "resolve_compose_file", lambda source_root: (source_root / "compose.yml", False)
    )
    restarted = []
    monkeypatch.setattr(
        docker_runtime_module, "restart_service",
        lambda compose_file, service: restarted.append(service),
    )
    removed = []
    monkeypatch.setattr(
        docker_runtime_module, "remove_old_images",
        lambda image_ids: removed.extend(image_ids),
    )

    result = invoke("update", "--docker", "--yes")
    assert result.exit_code == 0, result.output
    assert "pulled" in result.output
    assert "restarted" in result.output.lower()
    assert "cleaned up" in result.output.lower()
    assert set(restarted) == set(docker_runtime_module.RELEASE_IMAGES)
    assert removed == ["old-server-id", "old-webui-id"]


def test_update_docker_already_up_to_date(repo, monkeypatch):
    import python.av_cli.docker_runtime as docker_runtime_module

    monkeypatch.setattr(
        docker_runtime_module, "check_for_docker_update",
        lambda source_root: docker_runtime_module.DockerUpdateResult(
            checked=True, updated=False, message="Docker backend is already up to date.",
        ),
    )
    result = invoke("update", "--docker")
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


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
