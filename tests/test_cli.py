import json

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli
from python.av_cli.index import Index


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


# ---------------------------------------------------------------------------
# av init
# ---------------------------------------------------------------------------

def test_init_creates_av_structure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("init", "--mode", "local", "--yes", "--no-repl")
    assert result.exit_code == 0, result.output

    av_dir = tmp_path / ".av"
    assert (av_dir / "objects").is_dir()
    assert (av_dir / "refs" / "heads").is_dir()
    assert (av_dir / "commits").is_dir()
    assert av_dir.joinpath("HEAD").read_text().strip() == "ref: refs/heads/main"
    assert json.loads((av_dir / "config").read_text())["lfs_threshold_mb"] == 50
    assert json.loads((av_dir / "config").read_text())["login_mode"] == "local"


def test_init_twice_is_a_noop(repo):
    result = invoke("init", "--no-repl")
    assert result.exit_code == 0
    assert "already initialized" in result.output.lower()


def test_init_non_interactive_defaults_to_local(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("init", "--no-repl")
    assert result.exit_code == 0, result.output
    cfg = json.loads((tmp_path / ".av" / "config").read_text())
    assert cfg["login_mode"] == "local"


def test_init_enterprise_mode_shows_stub_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("init", "--mode", "enterprise", "--no-repl")
    assert result.exit_code == 0, result.output
    assert "coming soon" in result.output.lower()
    cfg = json.loads((tmp_path / ".av" / "config").read_text())
    assert cfg["login_mode"] == "local"  # stub falls back to local


# ---------------------------------------------------------------------------
# av init — Anonymous/Protected (--protected / --token)
# ---------------------------------------------------------------------------

def test_init_default_is_anonymous_no_token_saved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("init", "--mode", "local", "--yes", "--no-repl")
    assert result.exit_code == 0, result.output
    cfg = json.loads((tmp_path / ".av" / "config").read_text())
    assert "remote_api_token" not in cfg


def test_init_protected_flag_generates_and_saves_a_token(tmp_path, monkeypatch):
    import python.av_cli.docker_runtime as docker_runtime_module
    import python.av_cli.main as main_module

    monkeypatch.chdir(tmp_path)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(main_module, "_find_source_root", lambda: tmp_path)
    monkeypatch.setattr(docker_runtime_module, "check_docker_running", lambda: docker_runtime_module.DockerCheckResult.RUNNING)
    monkeypatch.setattr(docker_runtime_module, "restart_service", lambda *a, **k: True)

    result = invoke("init", "--mode", "local", "--protected", "--no-repl")
    assert result.exit_code == 0, result.output
    assert "Token set:" in result.output

    cfg = json.loads((tmp_path / ".av" / "config").read_text())
    assert cfg.get("remote_api_token")
    assert cfg["remote_api_token"] in (tmp_path / ".env").read_text(encoding="utf-8")


def test_init_token_flag_validates_and_saves_without_touching_env(tmp_path, monkeypatch):
    from python.av_cli.client import VaultClient

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(VaultClient, "server_available", lambda self: True)
    monkeypatch.setattr(VaultClient, "fetch_all_refs", lambda self: {})

    result = invoke("init", "--mode", "local", "--token", "teammates-token", "--no-repl")
    assert result.exit_code == 0, result.output
    assert "Token saved" in result.output

    cfg = json.loads((tmp_path / ".av" / "config").read_text())
    assert cfg["remote_api_token"] == "teammates-token"
    assert not (tmp_path / ".env").exists()  # join-existing never writes .env


def test_init_token_flag_rejected_token_is_not_saved(tmp_path, monkeypatch):
    from python.av_cli.client import AuthenticationError, VaultClient

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(VaultClient, "server_available", lambda self: True)

    def fake_fetch_all_refs(self):
        raise AuthenticationError("nope")

    monkeypatch.setattr(VaultClient, "fetch_all_refs", fake_fetch_all_refs)

    result = invoke("init", "--mode", "local", "--token", "wrong-token", "--no-repl")
    assert result.exit_code == 0, result.output
    assert "rejected" in result.output.lower()

    cfg = json.loads((tmp_path / ".av" / "config").read_text())
    assert "remote_api_token" not in cfg


def test_init_token_flag_saves_anyway_when_server_unreachable(tmp_path, monkeypatch):
    from python.av_cli.client import VaultClient

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(VaultClient, "server_available", lambda self: False)

    result = invoke("init", "--mode", "local", "--token", "some-token", "--no-repl")
    assert result.exit_code == 0, result.output
    assert "could not reach" in result.output.lower()

    cfg = json.loads((tmp_path / ".av" / "config").read_text())
    assert cfg["remote_api_token"] == "some-token"


# ---------------------------------------------------------------------------
# av add
# ---------------------------------------------------------------------------

def test_add_stages_small_file_without_pointer(repo):
    (repo / "train.py").write_text("print('hi')")
    result = invoke("add", "train.py")
    assert result.exit_code == 0, result.output
    assert "Staged" in result.output
    assert not (repo / "train.py.av-pointer").exists()

    idx = Index(repo)
    assert idx.get_entry("train.py")["type"] == "code"
    assert idx.get_staged_entries()["train.py"]["staged"] is True


def test_add_large_file_creates_pointer(repo):
    invoke("config", "1")  # 1 MB LFS threshold

    big = repo / "weights.pt"
    big.write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MB > 1 MB threshold

    result = invoke("add", "weights.pt")
    assert result.exit_code == 0, result.output
    assert (repo / "weights.pt.av-pointer").exists()

    idx = Index(repo)
    entry = idx.get_entry("weights.pt")
    assert entry["type"] == "artifact"
    obj_path = repo / ".av" / "objects" / entry["hash"][:2] / entry["hash"][2:]
    assert obj_path.exists()


def _make_safetensors(tensors: dict) -> bytes:
    """Builds a minimal valid safetensors blob: 8-byte LE header length + JSON header +
    data. Same format as tests/test_core.py's helper of the same name (kept duplicated
    here rather than imported across test modules, matching this file's existing
    self-contained-test convention)."""
    import json
    import struct

    header = {}
    offset = 0
    blobs = []
    for name, data in tensors.items():
        header[name] = {"dtype": "U8", "shape": [len(data)], "data_offsets": [offset, offset + len(data)]}
        offset += len(data)
        blobs.append(data)
    header_bytes = json.dumps(header).encode("utf-8")
    return struct.pack("<Q", len(header_bytes)) + header_bytes + b"".join(blobs)


def test_add_safetensors_skips_whole_file_copy_when_layers_split(repo):
    pytest.importorskip("aether_core")
    invoke("config", "1")  # 1 MB LFS threshold

    blob = _make_safetensors({"layer1": b"A" * (600 * 1024), "layer2": b"B" * (600 * 1024)})
    (repo / "model.safetensors").write_bytes(blob)

    result = invoke("add", "model.safetensors")
    assert result.exit_code == 0, result.output

    idx = Index(repo)
    entry = idx.get_entry("model.safetensors")
    assert entry["layers"], "expected layer-splitting to have produced layers"

    for layer in entry["layers"]:
        layer_obj = repo / ".av" / "objects" / layer["hash"][:2] / layer["hash"][2:]
        assert layer_obj.exists(), f"layer {layer['name']} object missing"

    # The whole-file blob must NOT be stored — storing it in addition to the layers would
    # defeat layer-dedup entirely (every fine-tune commit would re-store the full checkpoint
    # regardless of how many layers actually changed).
    whole_file_obj = repo / ".av" / "objects" / entry["hash"][:2] / entry["hash"][2:]
    assert not whole_file_obj.exists()


def test_checkout_reassembles_safetensors_from_layers(repo):
    pytest.importorskip("aether_core")
    invoke("config", "1")

    blob = _make_safetensors({"layer1": b"A" * (600 * 1024), "layer2": b"B" * (600 * 1024)})
    (repo / "model.safetensors").write_bytes(blob)
    invoke("add", "model.safetensors")
    invoke("commit", "-m", "v1")

    (repo / "model.safetensors").unlink()
    result = invoke("checkout", "main", "--force")
    assert result.exit_code == 0, result.output
    assert (repo / "model.safetensors").read_bytes() == blob


def test_doctor_does_not_flag_layered_artifact_as_orphaned(repo):
    pytest.importorskip("aether_core")
    invoke("config", "1")

    blob = _make_safetensors({"layer1": b"A" * (600 * 1024), "layer2": b"B" * (600 * 1024)})
    (repo / "model.safetensors").write_bytes(blob)
    invoke("add", "model.safetensors")

    result = invoke("doctor")
    assert result.exit_code == 0, result.output
    assert "missing their object" not in result.output
    assert "No orphaned pointer entries" in result.output


def test_doctor_detects_orphaned_layered_artifact_with_missing_layer(repo):
    pytest.importorskip("aether_core")
    invoke("config", "1")

    blob = _make_safetensors({"layer1": b"A" * (600 * 1024), "layer2": b"B" * (600 * 1024)})
    (repo / "model.safetensors").write_bytes(blob)
    invoke("add", "model.safetensors")

    idx = Index(repo)
    entry = idx.get_entry("model.safetensors")
    layer_hash = entry["layers"][0]["hash"]
    (repo / ".av" / "objects" / layer_hash[:2] / layer_hash[2:]).unlink()

    result = invoke("doctor")
    assert result.exit_code == 0, result.output
    assert "missing their object" in result.output
    assert "model.safetensors" in result.output


def test_add_unchanged_file_does_not_restage(repo):
    (repo / "train.py").write_text("print('hi')")
    invoke("add", "train.py")
    invoke("commit", "-m", "first")

    idx = Index(repo)
    assert idx.get_entry("train.py")["staged"] is False

    # Re-adding the same unchanged content must not flip staged back to True.
    result = invoke("add", "train.py")
    assert result.exit_code == 0
    idx = Index(repo)
    assert idx.get_entry("train.py")["staged"] is False


def test_add_noop_does_not_rewrite_index_file(repo):
    """A no-op `add` (nothing changed) must not touch .av/index at all.

    Re-hashing is already skipped via the size+mtime fast path; this checks the other half —
    that add() doesn't unconditionally call idx.save() even when zero entries changed.
    """
    (repo / "train.py").write_text("print('hi')")
    invoke("add", "train.py")
    invoke("commit", "-m", "first")

    index_path = repo / ".av" / "index"
    mtime_before = index_path.stat().st_mtime_ns

    result = invoke("add", "train.py")
    assert result.exit_code == 0
    assert index_path.stat().st_mtime_ns == mtime_before


# ---------------------------------------------------------------------------
# av file / .avignore
# ---------------------------------------------------------------------------

def test_file_avignore_writes_template(repo):
    result = invoke("file", "--avignore")
    assert result.exit_code == 0
    assert "Wrote" in result.output

    avignore_path = repo / ".avignore"
    assert avignore_path.exists()
    assert "venv/" in avignore_path.read_text()


def test_file_avignore_refuses_to_overwrite(repo):
    (repo / ".avignore").write_text("# my custom patterns\nmystuff/\n")
    result = invoke("file", "--avignore")
    assert result.exit_code == 0
    assert "already exists" in result.output.lower()
    assert "mystuff/" in (repo / ".avignore").read_text()


def test_file_with_no_flags_is_a_noop(repo):
    result = invoke("file")
    assert result.exit_code == 0
    assert "nothing to do" in result.output.lower()
    assert not (repo / ".avignore").exists()


def test_avignore_excludes_matching_dir_from_add_and_status(repo):
    (repo / ".avignore").write_text("venv/\n*.log\n")
    (repo / "venv").mkdir()
    (repo / "venv" / "site.py").write_text("ignored")
    (repo / "debug.log").write_text("ignored too")
    (repo / "real_code.py").write_text("print('keep me')")

    result = invoke("add", ".")
    assert result.exit_code == 0
    assert "venv" not in result.output
    assert "debug.log" not in result.output
    assert "real_code.py" in result.output

    status = invoke("status")
    assert "venv" not in status.output
    assert "debug.log" not in status.output


# ---------------------------------------------------------------------------
# av unstage
# ---------------------------------------------------------------------------

def test_unstage_with_nothing_staged(repo):
    result = invoke("unstage")
    assert result.exit_code == 0
    assert "nothing staged" in result.output.lower()


def test_unstage_new_file_makes_it_untracked_again(repo):
    (repo / "newfile.py").write_text("a = 1")
    invoke("add", "newfile.py")

    result = invoke("unstage")
    assert result.exit_code == 0
    assert "newfile.py" in result.output

    status = invoke("status")
    assert "Untracked files" in status.output
    assert "newfile.py" in status.output
    assert (repo / "newfile.py").read_text() == "a = 1"  # working tree untouched


def test_unstage_modified_tracked_file_shows_modified_again(repo):
    (repo / "train.py").write_text("v1")
    invoke("add", "train.py")
    invoke("commit", "-m", "first")

    (repo / "train.py").write_text("v2 dirty")
    invoke("add", "train.py")

    result = invoke("unstage")
    assert result.exit_code == 0

    status = invoke("status")
    assert "Changes not staged for commit" in status.output
    assert "train.py" in status.output
    assert (repo / "train.py").read_text() == "v2 dirty"  # working tree untouched


def test_unstage_single_path_leaves_others_staged(repo):
    (repo / "a.py").write_text("a")
    (repo / "b.py").write_text("b")
    invoke("add", "a.py", "b.py")

    result = invoke("unstage", "a.py")
    assert result.exit_code == 0

    idx = Index(repo)
    assert idx.get_entry("a.py") is None
    assert idx.get_entry("b.py")["staged"] is True

    status = invoke("status")
    assert "a.py" in status.output  # untracked again
    assert "b.py" in status.output  # still staged


def test_unstage_artifact_removes_pointer_sidecar(repo):
    invoke("config", "1")  # 1MB LFS threshold
    (repo / "weights.pt").write_bytes(b"x" * (2 * 1024 * 1024))
    invoke("add", "weights.pt")
    assert (repo / "weights.pt.av-pointer").exists()

    result = invoke("unstage")
    assert result.exit_code == 0
    assert not (repo / "weights.pt.av-pointer").exists()
    assert (repo / "weights.pt").exists()  # real artifact untouched


# ---------------------------------------------------------------------------
# av status
# ---------------------------------------------------------------------------

def test_status_clean_tree(repo):
    result = invoke("status")
    assert result.exit_code == 0
    assert "nothing to commit" in result.output.lower()


def test_status_reports_untracked_staged_modified(repo):
    (repo / "staged.py").write_text("a = 1")
    (repo / "modified.py").write_text("a = 1")
    invoke("add", "staged.py", "modified.py")
    invoke("commit", "-m", "baseline")

    # `modified.py` changes after the commit; `staged.py` stays staged again; `new.py` is untracked.
    invoke("add", "modified.py")  # nothing changed yet, no-op
    (repo / "modified.py").write_text("a = 2")
    (repo / "new.py").write_text("a = 3")
    (repo / "staged.py").write_text("a = 4")
    invoke("add", "staged.py")

    result = invoke("status")
    assert result.exit_code == 0
    assert "staged.py" in result.output
    assert "modified.py" in result.output
    assert "new.py" in result.output


# ---------------------------------------------------------------------------
# av commit
# ---------------------------------------------------------------------------

def test_commit_with_nothing_staged_is_noop(repo):
    result = invoke("commit", "-m", "empty")
    assert result.exit_code == 0
    assert "nothing to commit" in result.output.lower()
    assert list((repo / ".av" / "commits").iterdir()) == []


def test_commit_writes_commit_object_and_advances_ref(repo):
    (repo / "train.py").write_text("print('hi')")
    invoke("add", "train.py")
    result = invoke("commit", "-m", "first commit")
    assert result.exit_code == 0, result.output

    commit_hash = (repo / ".av" / "refs" / "heads" / "main").read_text().strip()
    assert commit_hash and commit_hash[:7] in result.output

    commit_file = repo / ".av" / "commits" / f"{commit_hash}.json"
    assert commit_file.exists()
    data = json.loads(commit_file.read_text())
    assert data["message"] == "first commit"
    assert "train.py" in data["tree"]


def test_commit_with_tags_and_metrics(repo):
    (repo / "train.py").write_text("print('hi')")
    invoke("add", "train.py")
    result = invoke("commit", "-m", "tagged", "--tag", "v1", "--metric", "sharpe=2.5")
    assert result.exit_code == 0, result.output

    commit_hash = (repo / ".av" / "refs" / "heads" / "main").read_text().strip()
    data = json.loads((repo / ".av" / "commits" / f"{commit_hash}.json").read_text())
    assert data["tags"] == ["v1"]
    assert data["metrics"] == {"sharpe": 2.5}

    registry = json.loads((repo / ".av" / "registry.json").read_text())
    assert registry["tags"] == ["v1"]
    assert registry["metrics"] == ["sharpe"]


def test_commit_queues_for_retry_instead_of_losing_it_when_token_is_rejected(repo, monkeypatch):
    # Regression test for a real bug found via manual debugging (development/Probleme.md):
    # server_available() is exempt from the auth gate (so it stays answerable with no
    # credentials), which means it returning True does NOT mean this client's token is valid.
    # upload_commit_objects() raising AuthenticationError used to propagate straight out of
    # `commit()`, skipping the queue_pending_push() fallback entirely — the commit was created
    # locally but silently never queued for retry, unlike every other kind of push failure.
    from python.av_cli.client import AuthenticationError, VaultClient

    monkeypatch.setattr(VaultClient, "server_available", lambda self: True)
    monkeypatch.setattr(
        VaultClient, "batch_check_objects",
        lambda self, hashes: (_ for _ in ()).throw(AuthenticationError("nope")),
    )

    (repo / "train.py").write_text("print('hi')")
    invoke("add", "train.py")
    result = invoke("commit", "-m", "should be queued, not lost")
    assert result.exit_code == 0, result.output
    assert "queued for retry" in result.output.lower()

    commit_hash = (repo / ".av" / "refs" / "heads" / "main").read_text().strip()
    pending = json.loads((repo / ".av" / "pending_push").read_text())
    assert any(p["commit_hash"] == commit_hash for p in pending)


def test_flush_pending_push_preserves_queue_on_auth_failure(repo, monkeypatch):
    # Companion regression test: flush_pending_push() must not lose *other* already-queued
    # commits either when a bad/stale token is hit partway through retrying them.
    from python.av_cli.client import AuthenticationError, VaultClient
    import python.av_cli.main as main_module

    monkeypatch.setattr(VaultClient, "server_available", lambda self: True)

    # Queue two commits directly (bypassing a real push attempt) the same way commit() would
    # after a push failure, so flush_pending_push() has something to retry.
    for seed in ("a", "b"):
        commit_hash = "f" * 63 + seed
        (repo / ".av" / "commits" / f"{commit_hash}.json").write_text(
            json.dumps({"hash": commit_hash, "tree": {}, "message": "x", "author": "t"})
        )
        main_module.queue_pending_push(repo, commit_hash, None)

    monkeypatch.setattr(
        VaultClient, "push_commit",
        lambda self, data: (_ for _ in ()).throw(AuthenticationError("nope")),
    )

    client = VaultClient("http://localhost:8000", "irrelevant")
    with pytest.raises(AuthenticationError):
        main_module.flush_pending_push(repo, client)

    pending = json.loads((repo / ".av" / "pending_push").read_text())
    assert len(pending) == 2  # neither queued commit was dropped


# ---------------------------------------------------------------------------
# av checkout
# ---------------------------------------------------------------------------

def test_checkout_restores_previous_commit(repo):
    (repo / "model.txt").write_text("version 1")
    invoke("add", "model.txt")
    invoke("commit", "-m", "v1")
    commit1 = (repo / ".av" / "refs" / "heads" / "main").read_text().strip()

    (repo / "model.txt").write_text("version 2")
    invoke("add", "model.txt")
    invoke("commit", "-m", "v2")

    result = invoke("checkout", commit1)
    assert result.exit_code == 0, result.output
    assert (repo / "model.txt").read_text() == "version 1"


def test_checkout_refuses_with_uncommitted_changes_without_force(repo):
    (repo / "model.txt").write_text("version 1")
    invoke("add", "model.txt")
    invoke("commit", "-m", "v1")
    commit1 = (repo / ".av" / "refs" / "heads" / "main").read_text().strip()

    (repo / "model.txt").write_text("dirty, never committed")
    result = invoke("checkout", commit1)
    assert "Error" in result.output
    assert (repo / "model.txt").read_text() == "dirty, never committed"

    result = invoke("checkout", commit1, "--force")
    assert result.exit_code == 0, result.output
    assert (repo / "model.txt").read_text() == "version 1"


# ---------------------------------------------------------------------------
# av doctor
# ---------------------------------------------------------------------------

def test_doctor_on_healthy_repo_reports_ok(repo):
    # No add/commit here: committing without a reachable server queues a pending-push entry
    # (the tool's intended offline-resilient behavior, see `commit`/`queue_pending_push`), which
    # would make "No commits pending push" a false assertion in this test environment. A fresh,
    # untouched repo is enough to exercise the structure/core/index/pointer/tmp-file checks.
    result = invoke("doctor")
    assert result.exit_code == 0, result.output
    assert "Repository found at" in result.output
    assert "missing repository structure" not in result.output.lower()
    assert "No orphaned pointer entries" in result.output
    assert "No commits pending push" in result.output
    assert "No *.tmp.* leftover files" in result.output


def test_doctor_detects_orphaned_pointer(repo):
    invoke("config", "1")
    big = repo / "weights.pt"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    invoke("add", "weights.pt")

    idx = Index(repo)
    entry = idx.get_entry("weights.pt")
    obj_path = repo / ".av" / "objects" / entry["hash"][:2] / entry["hash"][2:]
    obj_path.unlink()  # simulate a lost/corrupted CAS object

    result = invoke("doctor")
    assert result.exit_code == 0
    assert "missing their object" in result.output
    assert "weights.pt" in result.output


def test_doctor_outside_repo_errors_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("doctor")
    assert result.exit_code != 0
    assert "not an aether-vault repository" in result.output.lower()


def test_doctor_speed_prints_real_repo_timing_table(repo):
    result = invoke("doctor", "--speed")
    assert result.exit_code == 0, result.output
    assert "Speed diagnostics" in result.output
    assert "Index.load()" in result.output
    assert "load_config()" in result.output
    assert "iter_working_files()" in result.output
    assert "Storage stats" in result.output


def test_doctor_without_speed_has_no_speed_section(repo):
    result = invoke("doctor")
    assert result.exit_code == 0, result.output
    assert "Speed diagnostics" not in result.output


def test_doctor_fix_relinks_stale_pointer_when_object_intact(repo):
    invoke("config", "1")
    big = repo / "weights.pt"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    invoke("add", "weights.pt")

    idx = Index(repo)
    entry = idx.get_entry("weights.pt")
    idx.remove_entry("weights.pt")  # object + pointer file remain; only the index entry is lost

    result = invoke("doctor", "--fix")
    assert result.exit_code == 0, result.output
    assert "[FIXED]" in result.output
    assert "Re-linked weights.pt.av-pointer back into the index" in result.output

    restored = Index(repo).get_entry("weights.pt")
    assert restored is not None
    assert restored["hash"] == entry["hash"]


def test_doctor_fix_downloads_missing_object_from_server(repo, monkeypatch):
    invoke("config", "1")
    big = repo / "weights.pt"
    content = b"x" * (2 * 1024 * 1024)
    big.write_bytes(content)
    invoke("add", "weights.pt")

    idx = Index(repo)
    entry = idx.get_entry("weights.pt")
    obj_path = repo / ".av" / "objects" / entry["hash"][:2] / entry["hash"][2:]
    obj_path.unlink()  # simulate a lost/corrupted CAS object

    def fake_download(self, sha256_hash, dest_path):
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(content)
        return True

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.VaultClient, "server_available", lambda self: True)
    monkeypatch.setattr(main_module.VaultClient, "download_object", fake_download)

    result = invoke("doctor", "--fix")
    assert result.exit_code == 0, result.output
    assert "[FIXED]" in result.output
    assert "by downloading its object from the remote" in result.output
    assert obj_path.exists()


def test_doctor_fix_cannot_recover_truly_missing_object(repo, monkeypatch):
    invoke("config", "1")
    big = repo / "weights.pt"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    invoke("add", "weights.pt")

    idx = Index(repo)
    entry = idx.get_entry("weights.pt")
    obj_path = repo / ".av" / "objects" / entry["hash"][:2] / entry["hash"][2:]
    obj_path.unlink()

    # Force "no server reachable" explicitly rather than relying on the test environment
    # happening to have none running — a real av_server on localhost:8000 (e.g. for manual
    # benchmark/webui testing) would otherwise make this object recoverable and flip the
    # assertions below, exactly as it did when this test was last run with Docker up.
    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.VaultClient, "server_available", lambda self: False)

    result = invoke("doctor", "--fix")
    assert result.exit_code == 0, result.output
    assert "[WARN]" in result.output
    assert "could not recover" in result.output
    assert "weights.pt" in result.output
    assert not obj_path.exists()


def test_doctor_fix_clears_tmp_leftovers(repo):
    tmp_file = repo / ".av" / "index.tmp.abcd1234"
    tmp_file.write_text("garbage")

    result = invoke("doctor", "--fix")
    assert result.exit_code == 0, result.output
    assert "[FIXED]" in result.output
    assert "Removed 1 leftover temp file" in result.output
    assert not tmp_file.exists()


def test_doctor_fix_clears_unrecoverable_pending_push_entries(repo):
    pending_path = repo / ".av" / "pending_push"
    pending_path.write_text(json.dumps([{"commit_hash": "deadbeef" * 8, "ref_name": "main"}]))

    result = invoke("doctor", "--fix")
    assert result.exit_code == 0, result.output
    assert "[FIXED]" in result.output
    assert "Cleared 1 unrecoverable pending-push entry" in result.output
    assert not pending_path.exists()  # save_pending_push removes the file once the queue is empty


def test_doctor_without_fix_only_warns(repo):
    tmp_file = repo / ".av" / "index.tmp.abcd1234"
    tmp_file.write_text("garbage")
    pending_path = repo / ".av" / "pending_push"
    pending_path.write_text(json.dumps([{"commit_hash": "deadbeef" * 8, "ref_name": "main"}]))

    result = invoke("doctor")
    assert result.exit_code == 0, result.output
    assert "[FIXED]" not in result.output
    assert tmp_file.exists()
    assert pending_path.exists()


def test_doctor_fix_dry_run_previews_without_modifying(repo):
    invoke("config", "1")
    big = repo / "weights.pt"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    invoke("add", "weights.pt")
    idx = Index(repo)
    entry = idx.get_entry("weights.pt")
    idx.remove_entry("weights.pt")  # stale pointer, object intact

    tmp_file = repo / ".av" / "index.tmp.abcd1234"
    tmp_file.write_text("garbage")
    pending_path = repo / ".av" / "pending_push"
    pending_path.write_text(json.dumps([{"commit_hash": "deadbeef" * 8, "ref_name": "main"}]))

    obj_path = repo / ".av" / "objects" / entry["hash"][:2] / entry["hash"][2:]
    assert obj_path.exists()
    before_index = (repo / ".av" / "index").read_bytes()
    before_pending = pending_path.read_bytes()
    before_tmp = tmp_file.read_bytes()

    result = invoke("doctor", "--fix", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "[WOULD FIX]" in result.output
    assert "[FIXED]" not in result.output
    assert "(dry run" in result.output

    assert tmp_file.exists() and tmp_file.read_bytes() == before_tmp
    assert pending_path.exists() and pending_path.read_bytes() == before_pending
    assert (repo / ".av" / "index").read_bytes() == before_index
    assert obj_path.exists()


def test_doctor_dry_run_without_fix_is_a_noop(repo):
    result_plain = invoke("doctor")
    result_dry = invoke("doctor", "--dry-run")
    assert result_plain.exit_code == result_dry.exit_code == 0
    assert result_plain.output == result_dry.output


# ---------------------------------------------------------------------------
# av test
# ---------------------------------------------------------------------------
# The pytest invocation itself runs via subprocess.Popen (not subprocess.run) so its output can
# be streamed live and captured for the README test-badge update (see _update_readme_test_badge)
# — these tests fake Popen accordingly. npm/av-CLI calls inside `test_cmd` still go through
# subprocess.run and are faked the same way as before.

def _fake_pytest_popen(returncode=0, summary="5 passed in 0.01s\n", captured_calls=None):
    """Build a fake replacement for subprocess.Popen that mimics just enough of the real
    interface for test_cmd's `for line in process.stdout: ...; process.wait()` loop."""
    def _popen(args, cwd=None, stdout=None, stderr=None, text=None, bufsize=None):
        if captured_calls is not None:
            captured_calls.append({"args": args, "cwd": cwd})
        proc = type("FakePytestProcess", (), {})()
        proc.stdout = iter([summary])
        proc.returncode = returncode
        proc.wait = lambda: returncode
        return proc
    return _popen


def test_test_command_invokes_pytest(repo, monkeypatch):
    calls = []

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.subprocess, "Popen", _fake_pytest_popen(captured_calls=calls), raising=False)
    monkeypatch.setattr(main_module, "_update_readme_test_badge", lambda *a, **k: None)

    result = invoke("test")
    assert result.exit_code == 0, result.output
    assert calls[0]["args"][1:3] == ["-m", "pytest"]
    assert calls[0]["args"][0].endswith(("python", "python.exe"))


def test_test_command_forwards_dash_k_and_cov(repo, monkeypatch):
    calls = []

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.subprocess, "Popen", _fake_pytest_popen(captured_calls=calls), raising=False)
    monkeypatch.setattr(main_module, "_update_readme_test_badge", lambda *a, **k: None)

    result = invoke("test", "-k", "foo", "--cov")
    assert result.exit_code == 0, result.output
    assert "-k" in calls[0]["args"] and "foo" in calls[0]["args"]
    assert "--cov=python" in calls[0]["args"]


def test_test_command_missing_tests_dir_gives_clear_error(repo, monkeypatch, tmp_path):
    fake_source_root = tmp_path / "no-tests-here"
    fake_source_root.mkdir()

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module, "_find_source_root", lambda: fake_source_root)

    result = invoke("test")
    assert result.exit_code != 0
    assert "development install" in result.output.lower()


def test_test_command_webui_runs_npm_test_after_pytest(repo, monkeypatch):
    pytest_calls = []
    run_calls = []

    class FakeCompleted:
        returncode = 0

    def fake_run(args, cwd=None):
        run_calls.append({"args": args, "cwd": cwd})
        return FakeCompleted()

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.subprocess, "run", fake_run, raising=False)
    monkeypatch.setattr(main_module.subprocess, "Popen", _fake_pytest_popen(captured_calls=pytest_calls), raising=False)
    monkeypatch.setattr(main_module, "_update_readme_test_badge", lambda *a, **k: None)
    # Don't depend on this machine/CI runner actually having npm on PATH — fix the resolved
    # path so the test is deterministic either way.
    monkeypatch.setattr(main_module.shutil, "which", lambda name: r"C:\fake\npm.cmd")

    result = invoke("test", "--webui")
    assert result.exit_code == 0, result.output
    assert len(pytest_calls) == 1
    assert pytest_calls[0]["args"][1:3] == ["-m", "pytest"]
    assert len(run_calls) == 1
    assert run_calls[0]["args"] == [r"C:\fake\npm.cmd", "test"]
    assert str(run_calls[0]["cwd"]).endswith("webui")


def test_test_command_without_webui_only_runs_pytest(repo, monkeypatch):
    pytest_calls = []
    run_calls = []

    class FakeCompleted:
        returncode = 0

    def fake_run(args, cwd=None):
        run_calls.append(args)
        return FakeCompleted()

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.subprocess, "run", fake_run, raising=False)
    monkeypatch.setattr(main_module.subprocess, "Popen", _fake_pytest_popen(captured_calls=pytest_calls), raising=False)
    monkeypatch.setattr(main_module, "_update_readme_test_badge", lambda *a, **k: None)

    result = invoke("test")
    assert result.exit_code == 0, result.output
    assert len(pytest_calls) == 1
    assert len(run_calls) == 0


def test_test_command_webui_combines_nonzero_exit_code(repo, monkeypatch):
    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.subprocess, "Popen", _fake_pytest_popen(returncode=0), raising=False)
    monkeypatch.setattr(main_module, "_update_readme_test_badge", lambda *a, **k: None)

    def fake_run(args, cwd=None):
        return type("FakeCompleted", (), {"returncode": 1})()  # npm test fails

    monkeypatch.setattr(main_module.subprocess, "run", fake_run, raising=False)
    monkeypatch.setattr(main_module.shutil, "which", lambda name: r"C:\fake\npm.cmd")

    result = invoke("test", "--webui")
    assert result.exit_code != 0


def test_test_command_webui_missing_webui_dir_gives_clear_error(repo, monkeypatch, tmp_path):
    fake_source_root = tmp_path / "no-webui-here"
    (fake_source_root / "tests").mkdir(parents=True)

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module, "_find_source_root", lambda: fake_source_root)
    monkeypatch.setattr(main_module.subprocess, "Popen", _fake_pytest_popen(), raising=False)
    monkeypatch.setattr(main_module, "_update_readme_test_badge", lambda *a, **k: None)
    monkeypatch.setattr(
        main_module.subprocess,
        "run",
        lambda args, cwd=None: type("FakeCompleted", (), {"returncode": 0})(),
        raising=False,
    )

    result = invoke("test", "--webui")
    assert result.exit_code != 0
    assert "development install" in result.output.lower()


def test_test_command_speed_runs_synthetic_probes_and_forwards_durations(repo, monkeypatch):
    pytest_calls = []
    run_calls = []

    class FakeCompleted:
        returncode = 0

    def fake_run(args, cwd=None):
        run_calls.append(args)
        return FakeCompleted()

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.subprocess, "run", fake_run, raising=False)
    monkeypatch.setattr(main_module.subprocess, "Popen", _fake_pytest_popen(captured_calls=pytest_calls), raising=False)
    monkeypatch.setattr(main_module, "_update_readme_test_badge", lambda *a, **k: None)
    # Run with no real `av` on PATH, so the optional "av CLI, end-to-end" subsection is
    # skipped — this test is only about the internal synthetic probes and -k/--cov-style
    # flag forwarding to pytest, not the extra end-to-end subprocess timing.
    monkeypatch.setattr(main_module.shutil, "which", lambda name: None)

    result = invoke("test", "--speed")
    assert result.exit_code == 0, result.output
    assert len(pytest_calls) == 1  # pytest only — no av CLI found
    assert len(run_calls) == 0
    assert "--durations=20" in pytest_calls[0]["args"]
    assert "Speed check (synthetic fixtures)" in result.output
    assert "Index.save()" in result.output
    assert "Storage stats" in result.output
    assert "av CLI not found on PATH" in result.output


def test_test_command_speed_webui_runs_bench_after_npm_test(repo, monkeypatch):
    pytest_calls = []
    calls = []

    class FakeCompleted:
        returncode = 0

    def fake_run(args, cwd=None):
        calls.append({"args": args, "cwd": cwd})
        return FakeCompleted()

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.subprocess, "run", fake_run, raising=False)
    monkeypatch.setattr(main_module.subprocess, "Popen", _fake_pytest_popen(captured_calls=pytest_calls), raising=False)
    monkeypatch.setattr(main_module, "_update_readme_test_badge", lambda *a, **k: None)
    fake_paths = {"npm": r"C:\fake\npm.cmd", "av": r"C:\fake\av.cmd"}
    monkeypatch.setattr(main_module.shutil, "which", lambda name: fake_paths.get(name))

    result = invoke("test", "--speed", "--webui")
    assert result.exit_code == 0, result.output
    assert len(pytest_calls) == 1
    # npm test, npm run bench, then av init/add/commit for the end-to-end subsection
    assert len(calls) == 5
    assert calls[0]["args"] == [r"C:\fake\npm.cmd", "test"]
    assert calls[1]["args"] == [r"C:\fake\npm.cmd", "run", "bench"]
    assert calls[2]["args"][0] == r"C:\fake\av.cmd" and calls[2]["args"][1] == "init"
    assert calls[4]["args"][0] == r"C:\fake\av.cmd" and calls[4]["args"][1] == "commit"
    assert "Web UI speed bench" in result.output
    assert "Speed check (av CLI, end-to-end)" in result.output


_FAKE_BADGE_README = (
    '<img src="https://img.shields.io/badge/tests-161%2F164%20passing-brightgreen'
    '?style=flat-square&labelColor=1A1A1A" alt="161 of 164 tests passing">'
)


def test_update_readme_test_badge_rewrites_url_and_alt_text(tmp_path, monkeypatch):
    import python.av_cli.main as main_module
    (tmp_path / "README.md").write_text(_FAKE_BADGE_README, encoding="utf-8")
    monkeypatch.setattr(main_module, "_find_source_root", lambda: tmp_path)

    main_module._update_readme_test_badge(173, 0)

    text = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "tests-173%2F173%20passing-brightgreen" in text
    assert 'alt="173 of 173 tests passing"' in text


def test_update_readme_test_badge_turns_red_when_failures_present(tmp_path, monkeypatch):
    import python.av_cli.main as main_module
    (tmp_path / "README.md").write_text(_FAKE_BADGE_README, encoding="utf-8")
    monkeypatch.setattr(main_module, "_find_source_root", lambda: tmp_path)

    main_module._update_readme_test_badge(170, 3)

    text = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "tests-170%2F173%20passing-red" in text
    assert 'alt="170 of 173 tests passing"' in text


def test_update_readme_test_badge_is_a_noop_without_a_parsed_total(tmp_path, monkeypatch):
    import python.av_cli.main as main_module
    (tmp_path / "README.md").write_text(_FAKE_BADGE_README, encoding="utf-8")
    monkeypatch.setattr(main_module, "_find_source_root", lambda: tmp_path)

    main_module._update_readme_test_badge(0, 0)

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == _FAKE_BADGE_README


def test_test_command_updates_readme_badge_from_pytest_summary(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text(_FAKE_BADGE_README, encoding="utf-8")

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module, "_find_source_root", lambda: tmp_path)
    monkeypatch.setattr(
        main_module.subprocess,
        "Popen",
        _fake_pytest_popen(summary="2 failed, 171 passed, 5 skipped in 12.3s\n"),
        raising=False,
    )

    result = invoke("test")
    assert result.exit_code == 0, result.output  # the fake process's own returncode, not pytest's

    text = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "tests-171%2F173%20passing-red" in text
    assert 'alt="171 of 173 tests passing"' in text


def test_test_command_with_dash_k_does_not_touch_readme_badge(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text(_FAKE_BADGE_README, encoding="utf-8")

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module, "_find_source_root", lambda: tmp_path)
    monkeypatch.setattr(
        main_module.subprocess,
        "Popen",
        _fake_pytest_popen(summary="3 passed in 0.4s\n"),
        raising=False,
    )

    result = invoke("test", "-k", "foo")
    assert result.exit_code == 0, result.output

    # A -k-scoped run only exercises a subset of the suite — never let it overwrite the badge
    # with a misleadingly small total.
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == _FAKE_BADGE_README


# ---------------------------------------------------------------------------
# av benchmark
# ---------------------------------------------------------------------------
# Mocks the module dispatch (importlib.import_module + module.run), not real subprocess
# work — this suite only exercises av_cli's command-surface (--only/--vs/--markdown/error
# handling), not bench_*.py's own real-tool timing logic (those are exercised by running
# them directly, see benchmarks/README.md).

class _FakeBenchModule:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def run(self, tool_order):
        self.calls.append(tool_order)
        from benchmarks.tool_runner import BenchmarkResult, Row, ToolStatus
        return BenchmarkResult(
            name=self.name,
            title=f"Fake {self.name}",
            description="fake",
            tool_order=tool_order,
            rows=[Row(operation="op", values={t: 1.0 for t in tool_order}, statuses={t: ToolStatus.AVAILABLE for t in tool_order})],
        )


def test_benchmark_command_missing_benchmarks_dir_gives_clear_error(repo, monkeypatch, tmp_path):
    fake_source_root = tmp_path / "no-benchmarks-here"
    fake_source_root.mkdir()

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module, "_find_source_root", lambda: fake_source_root)

    result = invoke("benchmark")
    assert result.exit_code != 0
    assert "development install" in result.output.lower()


def test_benchmark_command_only_filters_to_named_benchmarks(repo, monkeypatch):
    import python.av_cli.main as main_module

    imported = []

    def fake_import(name):
        imported.append(name)
        return _FakeBenchModule(name)

    monkeypatch.setattr(main_module.importlib, "import_module", fake_import)

    result = invoke("benchmark", "--only", "hashing_throughput")
    assert result.exit_code == 0, result.output
    assert imported == ["benchmarks.bench_hashing_throughput"]


def test_benchmark_command_runs_all_by_default(repo, monkeypatch):
    import python.av_cli.main as main_module

    imported = []
    monkeypatch.setattr(main_module.importlib, "import_module", lambda name: (imported.append(name), _FakeBenchModule(name))[1])

    result = invoke("benchmark")
    assert result.exit_code == 0, result.output
    assert len(imported) == len(main_module.BENCHMARK_NAMES)


def test_benchmark_command_rejects_unknown_only_name(repo):
    result = invoke("benchmark", "--only", "not_a_real_benchmark")
    assert result.exit_code != 0
    assert "unknown benchmark" in result.output.lower()


def test_benchmark_command_rejects_unknown_vs_tool(repo):
    result = invoke("benchmark", "--vs", "not-a-real-tool")
    assert result.exit_code != 0
    assert "unknown --vs tool" in result.output.lower()


def test_benchmark_command_vs_filters_competitor_tools(repo, monkeypatch):
    import python.av_cli.main as main_module

    modules = []

    def fake_import(name):
        m = _FakeBenchModule(name)
        modules.append(m)
        return m

    monkeypatch.setattr(main_module.importlib, "import_module", fake_import)

    result = invoke("benchmark", "--only", "hashing_throughput", "--vs", "git-lfs")
    assert result.exit_code == 0, result.output
    assert modules[0].calls == [["av", "git-lfs"]]


def test_benchmark_command_markdown_writes_file(repo, monkeypatch, tmp_path):
    import python.av_cli.main as main_module
    import benchmarks.tool_runner as tool_runner_module

    monkeypatch.setattr(main_module.importlib, "import_module", lambda name: _FakeBenchModule(name))
    # render_doc_header() shells out to detect real tool versions (slow, and one tool — mlflow
    # — is known to hang under non-interactive subprocess invocation until its own internal
    # timeout fires) — fake it so this test stays fast and deterministic. Patched via the real
    # module object (imported above, before the importlib.import_module patch above takes
    # effect) rather than monkeypatch's string-target form — that form calls
    # importlib.import_module internally too, which the patch above would intercept and break.
    monkeypatch.setattr(tool_runner_module, "render_doc_header", lambda *a, **k: "# fake header\n\n")

    out_path = tmp_path / "BENCHMARKS.md"
    result = invoke("benchmark", "--only", "hashing_throughput", "--markdown", str(out_path))
    assert result.exit_code == 0, result.output
    assert out_path.exists()
    text = out_path.read_text()
    assert "# fake header" in text
    assert "Fake benchmarks.bench_hashing_throughput" in text


def test_benchmark_command_save_json_writes_a_snapshot(repo, monkeypatch, tmp_path):
    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.importlib, "import_module", lambda name: _FakeBenchModule(name))

    out_path = tmp_path / "snapshot.json"
    result = invoke("benchmark", "--only", "hashing_throughput", "--save-json", str(out_path))
    assert result.exit_code == 0, result.output
    assert out_path.exists()

    snapshot = json.loads(out_path.read_text())
    assert snapshot == {"benchmarks.bench_hashing_throughput": {"op": 1.0}}


def test_benchmark_command_baseline_exits_nonzero_on_a_real_regression(repo, monkeypatch, tmp_path):
    import python.av_cli.main as main_module

    class _RegressedBenchModule(_FakeBenchModule):
        def run(self, tool_order):
            self.calls.append(tool_order)
            from benchmarks.tool_runner import BenchmarkResult, Row, ToolStatus
            return BenchmarkResult(
                name=self.name,
                title=f"Fake {self.name}",
                description="fake",
                tool_order=tool_order,
                rows=[Row(operation="op", values={t: 100.0 for t in tool_order}, statuses={t: ToolStatus.AVAILABLE for t in tool_order})],
            )

    monkeypatch.setattr(main_module.importlib, "import_module", lambda name: _RegressedBenchModule(name))

    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"benchmarks.bench_hashing_throughput": {"op": 10.0}}))

    result = invoke("benchmark", "--only", "hashing_throughput", "--baseline", str(baseline_path))
    assert result.exit_code != 0
    assert "regress" in result.output.lower()


def test_benchmark_command_baseline_passes_when_nothing_regressed(repo, monkeypatch, tmp_path):
    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.importlib, "import_module", lambda name: _FakeBenchModule(name))

    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"benchmarks.bench_hashing_throughput": {"op": 1.0}}))

    result = invoke("benchmark", "--only", "hashing_throughput", "--baseline", str(baseline_path))
    assert result.exit_code == 0, result.output
    assert "no regressions" in result.output.lower()


def test_benchmark_command_accepts_gc_throughput_via_only(repo, monkeypatch):
    import python.av_cli.main as main_module

    imported = []
    monkeypatch.setattr(
        main_module.importlib, "import_module",
        lambda name: (imported.append(name), _FakeBenchModule(name))[1],
    )

    result = invoke("benchmark", "--only", "gc_throughput")
    assert result.exit_code == 0, result.output
    assert imported == ["benchmarks.bench_gc_throughput"]


# ---------------------------------------------------------------------------
# av auth
# ---------------------------------------------------------------------------
# Docker itself is never touched here — restart_service/check_docker_running are mocked, same
# convention as the docker_runtime tests for `av update --docker`.

def _sandbox_compose_dir(repo, monkeypatch):
    """`av auth set-token`/`clear` resolve a real compose file via _find_source_root() ->
    resolve_compose_file() and write a real .env next to it — without this, that would write
    into this *actual checkout's* docker-compose.yml directory (a real, dangerous side effect
    a manual run of these tests already caused once during development). Giving `repo` itself
    a dummy docker-compose.yml makes resolve_compose_file() treat it as the (fake) dev
    checkout, sandboxing every .env read/write to this test's own tmp_path.
    """
    import python.av_cli.main as main_module
    (repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(main_module, "_find_source_root", lambda: repo)


def test_auth_set_token_generates_one_when_omitted(repo, monkeypatch):
    import python.av_cli.docker_runtime as docker_runtime_module
    import python.av_cli.main as main_module

    _sandbox_compose_dir(repo, monkeypatch)
    monkeypatch.setattr(docker_runtime_module, "check_docker_running", lambda: docker_runtime_module.DockerCheckResult.RUNNING)
    monkeypatch.setattr(docker_runtime_module, "restart_service", lambda *a, **k: True)

    result = invoke("auth", "set-token")
    assert result.exit_code == 0, result.output
    assert "Token set:" in result.output

    cfg = main_module.load_config(repo)
    assert cfg.get("remote_api_token")
    env_text = (repo / ".env").read_text(encoding="utf-8")
    assert cfg["remote_api_token"] in env_text


def test_auth_set_token_with_explicit_value(repo, monkeypatch):
    import python.av_cli.docker_runtime as docker_runtime_module
    import python.av_cli.main as main_module

    _sandbox_compose_dir(repo, monkeypatch)
    monkeypatch.setattr(docker_runtime_module, "check_docker_running", lambda: docker_runtime_module.DockerCheckResult.RUNNING)
    monkeypatch.setattr(docker_runtime_module, "restart_service", lambda *a, **k: True)

    result = invoke("auth", "set-token", "my-explicit-token")
    assert result.exit_code == 0, result.output

    cfg = main_module.load_config(repo)
    assert cfg["remote_api_token"] == "my-explicit-token"


def test_auth_set_token_restarts_the_server(repo, monkeypatch):
    import python.av_cli.docker_runtime as docker_runtime_module

    _sandbox_compose_dir(repo, monkeypatch)
    monkeypatch.setattr(docker_runtime_module, "check_docker_running", lambda: docker_runtime_module.DockerCheckResult.RUNNING)
    restart_calls = []
    monkeypatch.setattr(
        docker_runtime_module, "restart_service",
        lambda compose_file, service: (restart_calls.append(service), True)[1],
    )

    invoke("auth", "set-token", "a-token")
    assert restart_calls == ["aether-vault-server"]


def test_auth_set_token_when_docker_not_running_warns_but_still_saves(repo, monkeypatch):
    import python.av_cli.docker_runtime as docker_runtime_module
    import python.av_cli.main as main_module

    _sandbox_compose_dir(repo, monkeypatch)
    monkeypatch.setattr(docker_runtime_module, "check_docker_running", lambda: docker_runtime_module.DockerCheckResult.NOT_RUNNING)

    result = invoke("auth", "set-token", "a-token")
    assert result.exit_code == 0, result.output
    assert "take effect next time" in result.output.lower()

    cfg = main_module.load_config(repo)
    assert cfg["remote_api_token"] == "a-token"


def test_auth_clear_removes_token_everywhere(repo, monkeypatch):
    import python.av_cli.docker_runtime as docker_runtime_module
    import python.av_cli.main as main_module

    _sandbox_compose_dir(repo, monkeypatch)
    monkeypatch.setattr(docker_runtime_module, "check_docker_running", lambda: docker_runtime_module.DockerCheckResult.RUNNING)
    monkeypatch.setattr(docker_runtime_module, "restart_service", lambda *a, **k: True)

    invoke("auth", "set-token", "a-token")
    result = invoke("auth", "clear")
    assert result.exit_code == 0, result.output
    assert "Anonymous" in result.output

    cfg = main_module.load_config(repo)
    assert "remote_api_token" not in cfg
    assert "AV_API_TOKEN" not in (repo / ".env").read_text(encoding="utf-8")


def test_auth_status_reports_anonymous_by_default(repo):
    result = invoke("auth", "status")
    assert result.exit_code == 0, result.output
    assert "Anonymous" in result.output


def test_auth_status_reports_protected_without_printing_the_token(repo, monkeypatch):
    import python.av_cli.docker_runtime as docker_runtime_module

    _sandbox_compose_dir(repo, monkeypatch)
    monkeypatch.setattr(docker_runtime_module, "check_docker_running", lambda: docker_runtime_module.DockerCheckResult.RUNNING)
    monkeypatch.setattr(docker_runtime_module, "restart_service", lambda *a, **k: True)

    invoke("auth", "set-token", "super-secret-value")
    result = invoke("auth", "status")
    assert result.exit_code == 0, result.output
    assert "Protected" in result.output
    assert "super-secret-value" not in result.output
    assert "alue" in result.output  # last 4 chars of the token are shown, masked
