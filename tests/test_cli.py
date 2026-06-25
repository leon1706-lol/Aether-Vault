import json

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
    result = invoke("init")
    assert result.exit_code == 0, result.output

    av_dir = tmp_path / ".av"
    assert (av_dir / "objects").is_dir()
    assert (av_dir / "refs" / "heads").is_dir()
    assert (av_dir / "commits").is_dir()
    assert av_dir.joinpath("HEAD").read_text().strip() == "ref: refs/heads/main"
    assert json.loads((av_dir / "config").read_text())["lfs_threshold_mb"] == 50


def test_init_twice_is_a_noop(repo):
    result = invoke("init")
    assert result.exit_code == 0
    assert "already initialized" in result.output.lower()


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


def test_doctor_fix_cannot_recover_truly_missing_object(repo):
    invoke("config", "1")
    big = repo / "weights.pt"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    invoke("add", "weights.pt")

    idx = Index(repo)
    entry = idx.get_entry("weights.pt")
    obj_path = repo / ".av" / "objects" / entry["hash"][:2] / entry["hash"][2:]
    obj_path.unlink()

    # No server running in this test environment, so the object genuinely cannot be recovered.
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

def test_test_command_invokes_pytest(repo, monkeypatch):
    calls = {}

    class FakeCompleted:
        returncode = 0

    def fake_run(args, cwd=None):
        calls["args"] = args
        calls["cwd"] = cwd
        return FakeCompleted()

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.subprocess, "run", fake_run, raising=False)

    result = invoke("test")
    assert result.exit_code == 0, result.output
    assert calls["args"][1:3] == ["-m", "pytest"]
    assert calls["args"][0].endswith(("python", "python.exe"))


def test_test_command_forwards_dash_k_and_cov(repo, monkeypatch):
    calls = {}

    class FakeCompleted:
        returncode = 0

    def fake_run(args, cwd=None):
        calls["args"] = args
        return FakeCompleted()

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.subprocess, "run", fake_run, raising=False)

    result = invoke("test", "-k", "foo", "--cov")
    assert result.exit_code == 0, result.output
    assert "-k" in calls["args"] and "foo" in calls["args"]
    assert "--cov=python" in calls["args"]


def test_test_command_missing_tests_dir_gives_clear_error(repo, monkeypatch, tmp_path):
    fake_source_root = tmp_path / "no-tests-here"
    fake_source_root.mkdir()

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module, "_find_source_root", lambda: fake_source_root)

    result = invoke("test")
    assert result.exit_code != 0
    assert "development install" in result.output.lower()
