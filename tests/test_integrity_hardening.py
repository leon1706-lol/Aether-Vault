"""Data-integrity and contract defects found by the 2026-11-05 audit (Probleme.md #195-#212)."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from python.av_cli import core
from python.av_cli.index import Index
from python.av_cli.main import cli

REPO_ROOT = Path(__file__).resolve().parents[1]


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def tip(repo: Path) -> str:
    return (repo / ".av" / "refs" / "heads" / "main").read_text().strip()


def commit_file(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text)
    invoke("add", name)
    result = invoke("commit", "-m", message)
    assert result.exit_code in (0, 13), result.output
    return tip(repo)


def object_path(repo: Path, commit: str, rel: str) -> Path:
    tree = json.loads((repo / ".av" / "commits" / f"{commit}.json").read_text())["tree"]
    digest = tree[rel]["hash"]
    return repo / ".av" / "objects" / digest[:2] / digest[2:]


# --- restore and checkout must not report a success that did not happen -------------------


def test_restore_of_a_missing_object_fails_and_records_nothing(repo, unreachable_client):
    first = commit_file(repo, "a.txt", "v1", "v1")
    commit_file(repo, "a.txt", "v2", "v2")
    object_path(repo, first, "a.txt").unlink()
    (repo / "a.txt").write_text("corrupt")
    before = dict(Index(repo).entries["a.txt"])

    result = invoke("--output", "json", "restore", "--source", first, "a.txt")

    assert result.exit_code == core.EXIT_VALIDATION, result.output
    assert json.loads(result.output.strip().splitlines()[-1])["ok"] is False
    assert (repo / "a.txt").read_text() == "corrupt"
    assert Index(repo).entries["a.txt"] == before


def test_restore_of_a_corrupt_object_fails(repo, unreachable_client):
    first = commit_file(repo, "a.txt", "v1", "v1")
    commit_file(repo, "a.txt", "v2", "v2")
    path = object_path(repo, first, "a.txt")
    path.chmod(0o644)
    path.write_text("not the bytes that were hashed")

    result = invoke("restore", "--source", first, "a.txt")

    assert result.exit_code == core.EXIT_VALIDATION, result.output


def test_checkout_with_a_missing_object_fails_before_touching_the_tree(repo, unreachable_client):
    first = commit_file(repo, "a.txt", "v1", "v1")
    commit_file(repo, "a.txt", "v2", "v2")
    head_text = (repo / ".av" / "HEAD").read_text()
    object_path(repo, first, "a.txt").unlink()

    result = invoke("checkout", first)

    assert result.exit_code == core.EXIT_VALIDATION, result.output
    assert (repo / "a.txt").read_text() == "v2"
    assert (repo / ".av" / "HEAD").read_text() == head_text


def test_restore_leaves_the_index_so_the_path_reads_as_modified(repo, unreachable_client):
    first = commit_file(repo, "a.txt", "v1", "v1")
    commit_file(repo, "a.txt", "v2", "v2")
    commit_file(repo, "b.txt", "other", "b")
    second_hash = Index(repo).entries["a.txt"]["hash"]

    assert invoke("restore", "--source", first, "a.txt").exit_code == 0

    idx = Index(repo)
    assert idx.entries["a.txt"]["hash"] == second_hash
    _, modified, _, _ = core.compute_status(repo, idx)
    assert "a.txt" in modified
    # the next unrelated commit must not silently carry the restored version
    commit_file(repo, "c.txt", "c", "c")
    tree = json.loads((repo / ".av" / "commits" / f"{tip(repo)}.json").read_text())["tree"]
    assert tree["a.txt"]["hash"] == second_hash


def test_checkout_refuses_to_overwrite_an_untracked_file(repo, unreachable_client):
    first = commit_file(repo, "a.txt", "v1", "v1")
    commit_file(repo, "b.txt", "tracked", "b")
    second = tip(repo)
    assert invoke("checkout", first).exit_code == 0
    (repo / "b.txt").write_text("my untracked work")

    refused = invoke("checkout", second)
    assert refused.exit_code == core.EXIT_VALIDATION, refused.output
    assert (repo / "b.txt").read_text() == "my untracked work"

    forced = invoke("checkout", second, "--force")
    assert forced.exit_code == 0, forced.output
    assert (repo / "b.txt").read_text() == "tracked"


def test_checkout_accepts_an_untracked_file_with_the_target_content(repo, unreachable_client):
    first = commit_file(repo, "a.txt", "v1", "v1")
    commit_file(repo, "b.txt", "same", "b")
    second = tip(repo)
    assert invoke("checkout", first).exit_code == 0
    (repo / "b.txt").write_text("same")

    assert invoke("checkout", second).exit_code == 0


# --- a commit is queued before its ref moves ------------------------------------------------


def test_commit_is_queued_before_the_ref_moves(repo, unreachable_client, monkeypatch):
    (repo / "a.txt").write_text("v1")
    invoke("add", "a.txt")
    seen: dict = {}
    real_write = core.atomic_write_text

    def spy(path, text, **kwargs):
        if Path(path).parent.name == "heads":
            seen["queue"] = list(core.load_pending_push(repo))
        return real_write(path, text, **kwargs)

    monkeypatch.setattr(core, "atomic_write_text", spy)
    result = invoke("commit", "-m", "v1")
    assert result.exit_code in (0, 13), result.output
    assert [entry["commit_hash"] for entry in seen["queue"]] == [tip(repo)]


def test_a_commit_killed_before_upload_stays_queued(repo, monkeypatch):
    from python.av_cli import client as client_module

    monkeypatch.setattr(client_module.VaultClient, "server_available", lambda self: True)

    class Killed(BaseException):
        pass

    def die(*_args, **_kwargs):
        raise Killed

    monkeypatch.setattr(core, "upload_commit_objects", die)
    (repo / "a.txt").write_text("v1")
    invoke("add", "a.txt")
    with pytest.raises(Killed):
        core.commit_staged(repo, "v1")
    assert [entry["commit_hash"] for entry in core.load_pending_push(repo)] == [tip(repo)]


def test_queue_holds_one_entry_per_commit(repo):
    core.queue_pending_push(repo, "abc", "p/main")
    core.queue_pending_push(repo, "abc", "p/main")
    assert len(core.load_pending_push(repo)) == 1


def test_no_upload_never_touches_the_network(repo, monkeypatch):
    from python.av_cli import client as client_module

    def forbid(self, *args, **kwargs):
        raise AssertionError("--no-upload reached the network")

    monkeypatch.setattr(client_module.VaultClient, "server_available", forbid)
    (repo / "a.txt").write_text("v1")
    invoke("add", "a.txt")
    core.commit_staged(repo, "first", defer_upload=True)
    (repo / "a.txt").write_text("v2")
    invoke("add", "a.txt")
    core.commit_staged(repo, "second", defer_upload=True)
    assert len(core.load_pending_push(repo)) == 2


def test_flush_keeps_an_entry_queued_while_it_ran(repo, unreachable_client, monkeypatch):
    from python.av_cli import client as client_module

    commit_file(repo, "a.txt", "v1", "first")
    monkeypatch.setattr(client_module.VaultClient, "server_available", lambda self: True)
    monkeypatch.setattr(client_module.VaultClient, "push_commit", lambda self, data: True)
    monkeypatch.setattr(client_module.VaultClient, "update_ref", lambda self, *a, **k: True)

    def upload(repo_root, client, tree, only_paths=None):
        core.queue_pending_push(repo, "arrived-meanwhile", None)
        return True

    monkeypatch.setattr(core, "upload_commit_objects", upload)
    from python.av_cli.client import VaultClient

    core.flush_pending_push(repo, VaultClient("http://localhost:1"))
    assert [entry["commit_hash"] for entry in core.load_pending_push(repo)] == ["arrived-meanwhile"]


# --- locks ----------------------------------------------------------------------------------

_WRITER = """
import os, sys
sys.path.insert(0, {root!r})
os.chdir({repo!r})
from click.testing import CliRunner
from python.av_cli.main import cli
name = sys.argv[1]
open(name, "w").write(name)
result = CliRunner().invoke(cli, ["add", name])
if result.exit_code != 0:
    print("add", result.exit_code, result.output)
    raise SystemExit(1)
# All writers share one index, so another's commit may already have taken this file: exit 11.
result = CliRunner().invoke(cli, ["commit", "-m", name])
if result.exit_code not in (0, 11, 13):
    print("commit", result.exit_code, result.output)
    raise SystemExit(1)
"""


def test_parallel_writers_leave_a_linear_history_with_both(repo):
    script = _WRITER.format(root=str(REPO_ROOT), repo=str(repo))
    env = {**os.environ, "AV_DAEMON_DISABLE": "1"}
    processes = [
        subprocess.Popen([sys.executable, "-c", script, name], env=env, stdout=subprocess.PIPE, text=True)
        for name in ("one.txt", "two.txt", "three.txt", "four.txt")
    ]
    outputs = [process.communicate(timeout=180)[0] for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs

    chain = []
    current = tip(repo)
    while current:
        chain.append(current)
        parents = json.loads((repo / ".av" / "commits" / f"{current}.json").read_text())["parents"]
        current = parents[0] if parents else None
    assert 1 <= len(chain) <= 4
    tree = json.loads((repo / ".av" / "commits" / f"{tip(repo)}.json").read_text())["tree"]
    assert set(tree) == {"one.txt", "two.txt", "three.txt", "four.txt"}
    assert {entry["commit_hash"] for entry in core.load_pending_push(repo)} == set(chain)


def test_lock_times_out_as_a_validation_error(repo, monkeypatch):
    monkeypatch.setenv("AV_LOCK_TIMEOUT", "0.2")
    script = (
        f"import sys, time; sys.path.insert(0, {str(REPO_ROOT)!r}); from pathlib import Path\n"
        "from python.av_cli.fsutil import RepoLock\n"
        f"lock = RepoLock(Path({str(repo)!r})); lock.acquire(); print('held', flush=True); time.sleep(20)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        (repo / "a.txt").write_text("v1")
        result = invoke("add", "a.txt")
        assert result.exit_code == core.EXIT_VALIDATION, result.output
        assert "locked" in result.output.lower()
    finally:
        holder.kill()
        holder.communicate()


def test_lock_is_released_when_its_holder_dies(repo):
    script = (
        f"import sys, os; sys.path.insert(0, {str(REPO_ROOT)!r}); from pathlib import Path\n"
        "from python.av_cli.fsutil import RepoLock\n"
        f"RepoLock(Path({str(repo)!r})).acquire(); os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=60)
    (repo / "a.txt").write_text("v1")
    assert invoke("add", "a.txt").exit_code == 0


# --- corrupt state is reported, never treated as empty -------------------------------------


def test_corrupt_index_stops_the_command_and_is_kept(repo):
    (repo / "a.txt").write_text("v1")
    invoke("add", "a.txt")
    invoke("commit", "-m", "v1")
    index_path = repo / ".av" / "index"
    index_path.write_text('{"entries": {"a.txt"')
    (repo / "b.txt").write_text("v1")

    result = invoke("--output", "json", "add", "b.txt")

    assert result.exit_code == core.EXIT_VALIDATION, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert envelope["ok"] is False and envelope["error"]["data"]["reason"] == "corrupt_state"
    assert index_path.read_text() == '{"entries": {"a.txt"'


def test_corrupt_queue_stops_a_commit_and_is_kept(repo):
    (repo / "a.txt").write_text("v1")
    invoke("add", "a.txt")
    queue_path = repo / ".av" / "pending_push"
    queue_path.write_text("[{broken")

    result = invoke("commit", "-m", "v1")

    assert result.exit_code == core.EXIT_VALIDATION, result.output
    assert queue_path.read_text() == "[{broken"


# --- av rm -----------------------------------------------------------------------------------


def test_rm_records_a_deletion_in_the_next_commit(repo, unreachable_client):
    commit_file(repo, "keep.txt", "k", "k")
    commit_file(repo, "gone.txt", "g", "g")
    before = tip(repo)

    result = invoke("rm", "gone.txt")
    assert result.exit_code == 0, result.output
    assert not (repo / "gone.txt").exists()

    committed = invoke("commit", "-m", "drop gone")
    assert committed.exit_code in (0, 13), committed.output
    assert tip(repo) != before
    tree = json.loads((repo / ".av" / "commits" / f"{tip(repo)}.json").read_text())["tree"]
    assert set(tree) == {"keep.txt"}
    staged, modified, deleted, _ = core.compute_status(repo, Index(repo))
    assert not staged and not modified and not deleted


def test_rm_cached_keeps_the_file_on_disk(repo, unreachable_client):
    commit_file(repo, "a.txt", "a", "a")
    assert invoke("rm", "--cached", "a.txt").exit_code == 0
    assert (repo / "a.txt").exists()
    assert "a.txt" not in Index(repo).entries


def test_rm_of_an_untracked_path_fails(repo):
    (repo / "a.txt").write_text("a")
    assert invoke("rm", "a.txt").exit_code == core.EXIT_VALIDATION
    assert (repo / "a.txt").exists()


def test_rm_records_a_file_already_gone_from_disk(repo, unreachable_client):
    commit_file(repo, "a.txt", "a", "a")
    commit_file(repo, "b.txt", "b", "b")
    (repo / "a.txt").unlink()
    assert invoke("rm", "a.txt").exit_code == 0
    assert invoke("commit", "-m", "drop").exit_code in (0, 13)
    assert invoke("checkout", tip(repo)).exit_code == 0


# --- JSON contract and exit codes ------------------------------------------------------------


def test_promote_across_branches_prints_one_envelope(repo, unreachable_client):
    commit_file(repo, "a.txt", "v1", "base")
    assert invoke("branch", "feature").exit_code == 0
    assert invoke("checkout", "feature").exit_code == 0
    commit_file(repo, "b.txt", "v1", "feature work")
    candidate = (repo / ".av" / "refs" / "heads" / "feature").read_text().strip()

    result = invoke("--output", "json", "promote", candidate, "--into", "main")

    assert result.exit_code in (0, 13), result.output
    lines = [line for line in result.output.strip().splitlines() if line.strip()]
    assert len(lines) == 1, result.output
    assert json.loads(lines[0])["meta"]["command"] == "promote"


def test_denied_promote_reports_policy_denied(repo, unreachable_client):
    (repo / "a.txt").write_text("v1")
    invoke("add", "a.txt")
    invoke("commit", "-m", "base", "--metric", "loss=0.5")
    assert invoke("branch", "feature").exit_code == 0
    assert invoke("checkout", "feature").exit_code == 0
    (repo / "b.txt").write_text("v1")
    invoke("add", "b.txt")
    invoke("commit", "-m", "worse", "--metric", "loss=0.9")
    candidate = (repo / ".av" / "refs" / "heads" / "feature").read_text().strip()
    assert invoke("policy", "set", "main", "loss", "<", "--threshold", "0.1").exit_code == 0

    result = invoke("--output", "json", "promote", candidate, "--into", "main")

    assert result.exit_code == core.EXIT_POLICY_DENIED, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert envelope["ok"] is False and envelope["error"]["code"] == "policy_denied"


def test_log_of_a_missing_branch_exits_non_zero_in_text_mode(repo):
    assert invoke("log", "--branch", "nope").exit_code == core.EXIT_VALIDATION


@pytest.mark.parametrize("raw, expected", [
    ("loss=1e-05", 1e-05), ("loss=0.5", 0.5), ("steps=3", 3), ("label=best", "best"),
])
def test_metric_values_parse(raw, expected):
    assert core.parse_metric_args((raw,)) == {raw.split("=")[0]: expected}


@pytest.mark.parametrize("raw", ["loss=nan", "loss=inf", "loss=-inf", "no_equals_sign", "=3"])
def test_bad_metric_values_are_refused(raw):
    with pytest.raises(core.ValidationError):
        core.parse_metric_args((raw,))


def test_metric_policy_on_a_text_value_denies_instead_of_crashing(repo, unreachable_client):
    (repo / "a.txt").write_text("v1")
    invoke("add", "a.txt")
    invoke("commit", "-m", "base", "--metric", "loss=0.5")
    assert invoke("branch", "feature").exit_code == 0
    assert invoke("checkout", "feature").exit_code == 0
    (repo / "b.txt").write_text("v1")
    invoke("add", "b.txt")
    invoke("commit", "-m", "text metric", "--metric", "loss=best")
    candidate = (repo / ".av" / "refs" / "heads" / "feature").read_text().strip()
    assert invoke("policy", "set", "main", "loss", "<", "--threshold", "0.9").exit_code == 0

    result = invoke("promote", candidate, "--into", "main")

    assert result.exit_code == core.EXIT_POLICY_DENIED, result.output


def test_download_progress_stays_out_of_json_output(repo, monkeypatch):
    from python.av_cli import client as client_module

    first = commit_file(repo, "a.txt", "v1", "v1")
    commit_file(repo, "a.txt", "v2", "v2")
    path = object_path(repo, first, "a.txt")
    content = path.read_bytes()
    path.unlink()

    def download(self, digest, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(content)
        return True

    monkeypatch.setattr(client_module.VaultClient, "server_available", lambda self: True)
    monkeypatch.setattr(client_module.VaultClient, "download_object", download)

    result = invoke("--output", "json", "restore", "--source", first, "a.txt")

    assert result.exit_code == 0, result.output
    json.loads(result.output.strip())


# --- ignore files as Windows writes them -----------------------------------------------------


@pytest.mark.parametrize("encoding, text", [
    ("utf-8-sig", "*.log\n"),
    ("utf-16", "*.log\n"),
    ("utf-8", "*.log   \n"),
])
def test_ignore_files_with_bom_utf16_or_trailing_spaces(repo, encoding, text):
    (repo / ".gitignore").write_bytes(text.encode(encoding))
    (repo / "a.log").write_text("x")
    (repo / "b.txt").write_text("x")
    _, _, _, untracked = core.compute_status(repo, Index(repo))
    assert "a.log" not in untracked and "b.txt" in untracked


def test_avignore_in_utf16_does_not_crash(repo):
    (repo / ".avignore").write_bytes("*.log\n".encode("utf-16"))
    (repo / "a.log").write_text("x")
    _, _, _, untracked = core.compute_status(repo, Index(repo))
    assert "a.log" not in untracked


def test_ignore_rules_agree_with_git_on_trailing_spaces_and_escapes(repo):
    if not shutil.which("git"):
        pytest.skip("git not available")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("trail.log   \nkeepspace\\ \n")
    rules = core.load_gitignore_rules(repo)
    for path in ("trail.log", "keepspace ", "keepspace"):
        git_says = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=repo).returncode == 0
        assert core._is_gitignored(path, path, False, rules) == git_says, path


def test_add_of_a_path_outside_the_repo_gives_an_envelope(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "x.txt"
    outside.write_text("x")

    result = invoke("--output", "json", "add", str(outside))

    assert result.exit_code == core.EXIT_VALIDATION, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert envelope["error"]["data"]["reason"] == "path_outside_repo"


def test_daemon_watches_the_cli_modules_for_staleness():
    from python.av_cli.daemon import DaemonServer

    server = DaemonServer.__new__(DaemonServer)
    watched = {path.name for path in server._watched_files()}
    assert {"core.py", "cmd_history.py", "__init__.py"} <= watched


# --- backup restore across pg_dump versions --------------------------------------------------


@pytest.mark.parametrize("code, stderr, failed", [
    (0, "", False),
    (1, 'pg_restore: error: could not execute query: ERROR:  unrecognized configuration parameter "transaction_timeout"\n'
        "Command was: SET transaction_timeout = 0;\npg_restore: warning: errors ignored on restore: 1\n", False),
    (1, 'ERROR:  unrecognized configuration parameter "transaction_timeout"\nERROR:  relation "x" does not exist\n', True),
    (1, "pg_restore: error: connection failed\n", True),
])
def test_pg_restore_tolerates_only_a_newer_clients_unknown_setting(code, stderr, failed):
    from python.av_cli.cmd_admin import _pg_restore_failed

    assert _pg_restore_failed(code, stderr) is failed


def _queued_chain(tmp_path, monkeypatch):
    """Three commits on main, each queued, with a parent chain like a real one."""
    import json

    from av_cli import core

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".av" / "commits").mkdir(parents=True)
    hashes = ["a" * 64, "b" * 64, "c" * 64]
    for index, commit_hash in enumerate(hashes):
        parents = [hashes[index - 1]] if index else []
        (tmp_path / ".av" / "commits" / f"{commit_hash}.json").write_text(json.dumps({"hash": commit_hash, "parents": parents, "tree": {}}), encoding="utf-8")
    (tmp_path / ".av" / "pending_push").write_text(json.dumps([{"commit_hash": commit_hash, "ref_name": "main"} for commit_hash in hashes]), encoding="utf-8")
    monkeypatch.setattr(core, "upload_commit_objects", lambda repo_root, client, tree: True)
    return core, hashes


class _RaceClient:
    """A server whose ref is already at `current`, so every compare-and-swap below it loses."""

    def __init__(self, current):
        self.current = current

    def server_available(self):
        return True

    def push_commit(self, commit):
        return True

    def update_ref(self, ref_name, commit_hash, expected_hash=None):
        from av_cli.client import RefRaceError

        if expected_hash == self.current:
            self.current = commit_hash
            return True
        raise RefRaceError(ref_name, self.current, expected_hash or "")


def test_a_commit_the_server_took_before_it_died_leaves_the_queue_on_the_retry(tmp_path, monkeypatch):
    # The ref moved to a, and the server died before the answer arrived: a's own retry loses to its own earlier write.
    core, hashes = _queued_chain(tmp_path, monkeypatch)
    assert core.flush_pending_push(tmp_path, _RaceClient(hashes[0])) == []


def test_commits_the_server_has_passed_leave_the_queue_too(tmp_path, monkeypatch):
    # A client killed mid-flush: the ref is already at b, and a and b were never taken off the queue.
    core, hashes = _queued_chain(tmp_path, monkeypatch)
    assert core.flush_pending_push(tmp_path, _RaceClient(hashes[1])) == []


def test_a_real_race_stays_queued(tmp_path, monkeypatch):
    # Someone else's commit holds the ref, and it isn't in this repo's history: nothing here was delivered.
    core, hashes = _queued_chain(tmp_path, monkeypatch)
    left = core.flush_pending_push(tmp_path, _RaceClient("f" * 64))
    assert [entry["commit_hash"] for entry in left] == hashes
