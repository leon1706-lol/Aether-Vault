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


def test_test_command_webui_runs_npm_test_after_pytest(repo, monkeypatch):
    calls = []

    class FakeCompleted:
        returncode = 0

    def fake_run(args, cwd=None):
        calls.append({"args": args, "cwd": cwd})
        return FakeCompleted()

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.subprocess, "run", fake_run, raising=False)
    # Don't depend on this machine/CI runner actually having npm on PATH — fix the resolved
    # path so the test is deterministic either way.
    monkeypatch.setattr(main_module.shutil, "which", lambda name: r"C:\fake\npm.cmd")

    result = invoke("test", "--webui")
    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert calls[0]["args"][1:3] == ["-m", "pytest"]
    assert calls[1]["args"] == [r"C:\fake\npm.cmd", "test"]
    assert str(calls[1]["cwd"]).endswith("webui")


def test_test_command_without_webui_only_runs_pytest(repo, monkeypatch):
    calls = []

    class FakeCompleted:
        returncode = 0

    def fake_run(args, cwd=None):
        calls.append(args)
        return FakeCompleted()

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.subprocess, "run", fake_run, raising=False)

    result = invoke("test")
    assert result.exit_code == 0, result.output
    assert len(calls) == 1


def test_test_command_webui_combines_nonzero_exit_code(repo, monkeypatch):
    call_count = {"n": 0}

    def fake_run(args, cwd=None):
        call_count["n"] += 1
        returncode = 0 if call_count["n"] == 1 else 1  # pytest passes, npm test fails
        return type("FakeCompleted", (), {"returncode": returncode})()

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.subprocess, "run", fake_run, raising=False)
    monkeypatch.setattr(main_module.shutil, "which", lambda name: r"C:\fake\npm.cmd")

    result = invoke("test", "--webui")
    assert result.exit_code != 0


def test_test_command_webui_missing_webui_dir_gives_clear_error(repo, monkeypatch, tmp_path):
    fake_source_root = tmp_path / "no-webui-here"
    (fake_source_root / "tests").mkdir(parents=True)

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module, "_find_source_root", lambda: fake_source_root)
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
    calls = []

    class FakeCompleted:
        returncode = 0

    def fake_run(args, cwd=None):
        calls.append(args)
        return FakeCompleted()

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.subprocess, "run", fake_run, raising=False)
    # Run with no real `av` on PATH, so the optional "av CLI, end-to-end" subsection is
    # skipped — this test is only about the internal synthetic probes and -k/--cov-style
    # flag forwarding to pytest, not the extra end-to-end subprocess timing.
    monkeypatch.setattr(main_module.shutil, "which", lambda name: None)

    result = invoke("test", "--speed")
    assert result.exit_code == 0, result.output
    assert len(calls) == 1  # pytest only — no av CLI found
    assert "--durations=20" in calls[0]
    assert "Speed check (synthetic fixtures)" in result.output
    assert "Index.save()" in result.output
    assert "Storage stats" in result.output
    assert "av CLI not found on PATH" in result.output


def test_test_command_speed_webui_runs_bench_after_npm_test(repo, monkeypatch):
    calls = []

    class FakeCompleted:
        returncode = 0

    def fake_run(args, cwd=None):
        calls.append({"args": args, "cwd": cwd})
        return FakeCompleted()

    import python.av_cli.main as main_module
    monkeypatch.setattr(main_module.subprocess, "run", fake_run, raising=False)
    fake_paths = {"npm": r"C:\fake\npm.cmd", "av": r"C:\fake\av.cmd"}
    monkeypatch.setattr(main_module.shutil, "which", lambda name: fake_paths.get(name))

    result = invoke("test", "--speed", "--webui")
    assert result.exit_code == 0, result.output
    # pytest, npm test, npm run bench, then av init/add/commit for the end-to-end subsection
    assert len(calls) == 6
    assert calls[1]["args"] == [r"C:\fake\npm.cmd", "test"]
    assert calls[2]["args"] == [r"C:\fake\npm.cmd", "run", "bench"]
    assert calls[3]["args"][0] == r"C:\fake\av.cmd" and calls[3]["args"][1] == "init"
    assert calls[5]["args"][0] == r"C:\fake\av.cmd" and calls[5]["args"][1] == "commit"
    assert "Web UI speed bench" in result.output
    assert "Speed check (av CLI, end-to-end)" in result.output


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
    monkeypatch.setattr(main_module.importlib, "import_module", lambda name: _FakeBenchModule(name))

    out_path = tmp_path / "BENCHMARKS.md"
    result = invoke("benchmark", "--only", "hashing_throughput", "--markdown", str(out_path))
    assert result.exit_code == 0, result.output
    assert out_path.exists()
    assert "Fake benchmarks.bench_hashing_throughput" in out_path.read_text()
