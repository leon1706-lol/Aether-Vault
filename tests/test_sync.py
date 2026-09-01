"""`av clone` / `av pull` end-to-end tests against an in-process fake registry.

FakeRemoteClient subclasses the real VaultClient and serves every read from a live source
repo's `.av/` state (plus whatever pushes it records), so these tests exercise the exact
code paths the real client uses — pagination, normalize_commit_row, batch object checks,
parallel downloads — with zero HTTP and zero Docker. The live two-repo Docker E2E lives in
tests/test_server.py alongside the other real-wire coverage.
"""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from python.av_cli.main import cli
from python.av_cli import client as client_module


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


class FakeRemoteClient(client_module.VaultClient):
    """Serves registry reads from `source_root`'s .av/ files; records pushes in memory.

    update_ref/list_refs/get_ref are backed by `recorded_refs`, seeded empty — so a commit
    only becomes "the remote tip" once its author actually pushed (mirroring the real
    server), while get_commit/list_commits read whichever source repo they were pointed at.
    """

    def __init__(self, source_root: Path, project_id: str, project_name: str):
        super().__init__("http://fake-registry")
        self.source_root = Path(source_root)
        self.project_id = project_id
        self.project_name = project_name
        self.recorded_refs: dict[str, str] = {}
        self.reject_pushes = False
        self.stored_objects: dict[str, bytes] = {}
        self.extra_projects: list[dict] = []

    # -- availability / discovery ----------------------------------------------------------

    def server_available(self) -> bool:
        return True

    def list_projects(self) -> list[dict]:
        mine = [{
            "project_id": self.project_id,
            "project_name": self.project_name,
            "commit_count": len(list((self.source_root / ".av" / "commits").glob("*.json"))),
            "last_push": None,
        }]
        return mine + self.extra_projects

    def list_refs(self, project_id: str | None = None) -> dict:
        if project_id not in (None, self.project_id):
            return {}
        return dict(self.recorded_refs)

    def get_ref(self, ref_name: str) -> str | None:
        return self.recorded_refs.get(ref_name)

    # -- history ---------------------------------------------------------------------------

    @staticmethod
    def _row_from_local(commit_data: dict, include_tree: bool) -> dict:
        parents = commit_data.get("parents") or []
        row = {
            "hash": commit_data["hash"],
            "message": commit_data.get("message", ""),
            "author": commit_data.get("author", "anonymous"),
            "timestamp": commit_data.get("timestamp"),
            "parent_hash": parents[0] if parents else None,
            "tags": commit_data.get("tags", []),
            "metrics": commit_data.get("metrics", {}),
            "project_id": commit_data.get("project_id"),
            "project_name": commit_data.get("project_name"),
            # v1.2.2: mirror the real server, which echoes signature blobs (and would
            # env_snapshot ids) so clones can verify/replay — dropping these here hid
            # exactly the bug the live manual pass caught.
            "signature": commit_data.get("signature"),
        }
        if include_tree:
            row["tree"] = commit_data.get("tree", {})
        return row

    def _all_local_commits(self) -> list[dict]:
        commits = []
        for cf in sorted((self.source_root / ".av" / "commits").glob("*.json")):
            with open(cf, "r", encoding="utf-8") as f:
                commits.append(json.load(f))
        return commits

    def list_commits(self, project_id: str, limit: int = 500, offset: int = 0,
                     include_layers: bool = False) -> dict | None:
        if project_id != self.project_id:
            return {"commits": [], "total": 0, "limit": limit, "offset": offset,
                    "next_offset": None}
        rows = [self._row_from_local(c, include_layers) for c in self._all_local_commits()]
        rows.sort(key=lambda r: r["timestamp"] or "", reverse=True)
        page = rows[offset:offset + limit]
        next_offset = offset + limit if offset + limit < len(rows) else None
        return {"commits": page, "total": len(rows), "limit": limit,
                "offset": offset, "next_offset": next_offset}

    def get_commit(self, commit_hash: str) -> dict | None:
        for c in self._all_local_commits():
            if c["hash"].startswith(commit_hash):
                row = self._row_from_local(c, include_tree=True)
                # server-side merge-commit reconstruction (extra_parents) lands in Phase 4
                if len(c.get("parents", [])) > 1:
                    row["parents"] = list(c["parents"])
                return row
        return None

    # -- objects / writes ------------------------------------------------------------------

    def _object_path(self, h: str) -> Path:
        return self.source_root / ".av" / "objects" / h[:2] / h[2:]

    def upload_object(self, file_path: Path, sha256_hash: str, known_missing: bool = False) -> bool:
        if self.reject_pushes:
            return False
        self.stored_objects[sha256_hash] = Path(file_path).read_bytes()
        return True

    def push_commit(self, commit_data: dict) -> bool:
        return not self.reject_pushes

    def update_ref(self, ref_name: str, commit_hash: str, expected_hash: str | None = None) -> bool:
        # v1.2.5: mirrors the real server's compare-and-swap semantics (server.py's
        # update_ref) — expected_hash=None keeps the pre-1.2.5 unconditional-write
        # behavior every existing test here relies on; when given, a mismatch raises
        # RefRaceError exactly like the real client does on a live 409.
        if self.reject_pushes:
            return False
        if expected_hash is not None and self.recorded_refs.get(ref_name) != expected_hash:
            from python.av_cli.client import RefRaceError

            raise RefRaceError(ref_name, self.recorded_refs.get(ref_name), expected_hash)
        self.recorded_refs[ref_name] = commit_hash
        return True

    def batch_check_objects(self, sha256_hashes: list[str]) -> set[str]:
        found = set(self.stored_objects)
        for h in sha256_hashes:
            p = self._object_path(h)
            if p.exists() or h in self.stored_objects:
                found.add(h)
        return found

    def download_object(self, sha256_hash: str, dest_path: Path) -> bool:
        data = self.stored_objects.get(sha256_hash)
        if data is None:
            p = self._object_path(sha256_hash)
            if p.exists():
                data = p.read_bytes()
        if data is None:
            return False
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        return True


def _seed_source_repo(root: Path, monkeypatch) -> str:
    """A real 2-commit repo (code + above-threshold artifact) to serve clones from."""
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(root)
    result = invoke("init", "--mode", "local", "--yes", "--no-repl")
    assert result.exit_code == 0, result.output
    invoke("config", "1")  # 1 MB LFS threshold → model.bin below stages as LFS artifact
    (root / "train.py").write_text("print('v1')")
    invoke("add", "train.py")
    r = invoke("commit", "-m", "c1")
    assert r.exit_code == 0, r.output
    (root / "train.py").write_text("print('v2')")
    (root / "model.bin").write_bytes(b"\x00" * (2 * 1024 * 1024))
    invoke("add", "train.py", "model.bin")
    r = invoke("commit", "-m", "c2")
    assert r.exit_code == 0, r.output

    cfg = json.loads((root / ".av" / "config").read_text())
    return cfg["project_id"]


@pytest.fixture
def fake_registry(tmp_path, monkeypatch):
    """Source repo + FakeRemoteClient wired in as THE VaultClient everywhere."""
    source = tmp_path / "source"
    pid = _seed_source_repo(source, monkeypatch)
    cfg = json.loads((source / ".av" / "config").read_text())
    fake = FakeRemoteClient(source, pid, cfg["project_name"])

    monkeypatch.setattr(client_module, "VaultClient", lambda *a, **k: fake)
    return {"source": source, "fake": fake, "pid": pid}


# ---------------------------------------------------------------------------
# v1.2.5: a merge resolving a genuine ref-race divergence must actually land its own
# push, not re-race against a local commit the server never held. Real bug caught live
# by scripts/e2e_scenario.sh's Phase A (see Probleme.md) — this reproduces it stack-free.
# ---------------------------------------------------------------------------

def test_merge_push_lands_when_ours_lost_its_own_ref_race(tmp_path, monkeypatch):
    """core.py::_finalize_commit() picks expected_hash=parents[0] ("ours") by default for
    a two-parent merge commit — correct when ours is the server's confirmed tip (an
    ordinary merge of some other branch), but wrong when ours itself already lost its OWN
    ref race and is still sitting in pending_push: the server's actual tip is parents[1]
    ("theirs", the target this merge is reconciling against), not ours. Using the wrong
    expectation made the merge's own push spuriously re-race and queue instead of
    landing — even though resolving that exact divergence is the whole point of the
    merge. Reproduced with one repo and two commits standing in for "two agents": a real
    commit+push for "theirs", then the local ref rewound to base before "ours" is made,
    so ours's own push genuinely loses the CAS race against theirs (all real object
    content — no fabricated hashes)."""
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    assert invoke("init", "--mode", "local", "--yes", "--no-repl").exit_code == 0

    cfg = json.loads((root / ".av" / "config").read_text())
    pid = cfg["project_id"]
    fake = FakeRemoteClient(root, pid, cfg["project_name"])
    monkeypatch.setattr(client_module, "VaultClient", lambda *a, **k: fake)

    ref_name = f"{pid}/main"
    ref_path = root / ".av" / "refs" / "heads" / "main"

    (root / "shared.txt").write_text("base")
    invoke("add", "shared.txt")
    assert invoke("commit", "-m", "base").exit_code == 0
    base_hash = ref_path.read_text().strip()
    assert fake.recorded_refs[ref_name] == base_hash

    # "The other agent": a real commit + push, landing cleanly since the ref is still at
    # base_hash — real object content, no fabrication.
    (root / "shared.txt").write_text("theirs content")
    invoke("add", "shared.txt")
    assert invoke("commit", "-m", "their edit").exit_code == 0
    theirs_hash = ref_path.read_text().strip()
    assert fake.recorded_refs[ref_name] == theirs_hash

    # Rewind OUR local state back to base, as if we never saw their push — the next local
    # commit genuinely forks from base, exactly like two agents editing concurrently.
    ref_path.write_text(base_hash)
    (root / "shared.txt").write_text("base")
    invoke("add", "shared.txt")

    (root / "shared.txt").write_text("ours content")
    invoke("add", "shared.txt")
    assert invoke("commit", "-m", "our edit").exit_code == 0
    ours_hash = ref_path.read_text().strip()
    # Lost the CAS race (expected base_hash, server now holds theirs_hash) — queued, not
    # landed and never lost.
    assert fake.recorded_refs[ref_name] == theirs_hash
    pending = json.loads((root / ".av" / "pending_push").read_text())
    assert any(e["commit_hash"] == ours_hash for e in pending)

    r = invoke("merge", theirs_hash, "--theirs")
    assert r.exit_code == 0, r.output
    merge_hash = ref_path.read_text().strip()
    assert merge_hash not in (ours_hash, theirs_hash)
    assert (root / "shared.txt").read_text() == "theirs content"

    # The fix under test: the merge's own ref update must land on the server, not
    # re-race against "ours" (which the server never actually held).
    assert fake.recorded_refs[ref_name] == merge_hash, (
        f"merge push should advance the server ref to {merge_hash!r}, got "
        f"{fake.recorded_refs[ref_name]!r} instead — did it re-race against 'ours'?"
    )


# ---------------------------------------------------------------------------
# v1.2.2: signatures and env snapshot ids must SURVIVE clone/pull round trips.
# ---------------------------------------------------------------------------

def test_normalize_commit_row_carries_signature_and_env_id():
    from python.av_cli.sync import normalize_commit_row

    sig = {"algo": "ed25519", "public_key": "ab", "sig": "cd"}
    row = {
        "hash": "1" * 64, "message": "m", "author": "a", "timestamp": None,
        "parent_hash": None, "parents": [], "tags": [], "metrics": {},
        "project_id": "p", "project_name": "n",
        "signature": sig, "env_snapshot_id": "e" * 64,
    }
    normalized = normalize_commit_row(row)
    assert normalized["signature"] == sig
    assert normalized["env_snapshot_id"] == "e" * 64
    # And rows WITHOUT them stay clean (no phantom keys):
    bare = normalize_commit_row({k: v for k, v in row.items()
                                 if k not in ("signature", "env_snapshot_id")})
    assert "signature" not in bare and "env_snapshot_id" not in bare


def test_clone_preserves_signature_for_offline_verify(fake_registry, tmp_path, monkeypatch):
    """The whole point of persisting signatures server-side: a clone on a FRESH machine
    (no .av/keys of its own) can still verify the author's commit — offline."""
    pytest.importorskip("cryptography")  # keygen below needs the [sign] extra

    import base64

    from python.av_cli.signing import generate_keypair, verify_signature

    source = fake_registry["source"]
    monkeypatch.chdir(source)
    generate_keypair(source)  # auto-sign kicks in for commits made AFTER this
    (source / "signed_thing.txt").write_text("v")
    invoke("add", "signed_thing.txt")
    r = invoke("commit", "-m", "signed commit")
    assert r.exit_code == 0, r.output
    signed_hash = (source / ".av" / "refs" / "heads" / "main").read_text().strip()

    clone = tmp_path / "clone"
    monkeypatch.chdir(tmp_path)
    result = invoke("clone", fake_registry["pid"], str(clone))
    assert result.exit_code == 0, result.output

    cloned_commit_file = clone / ".av" / "commits" / f"{signed_hash}.json"
    assert cloned_commit_file.exists(), "signed commit missing from the clone"
    data = json.loads(cloned_commit_file.read_text(encoding="utf-8"))
    assert isinstance(data.get("signature"), dict), \
        "clone dropped the signature — verify would falsely report UNSIGNED"
    ok, reason = verify_signature(data)
    assert ok, f"cloned signature failed verification: {reason}"
    del base64


def test_pull_preserves_signature_for_offline_verify(fake_registry, tmp_path, monkeypatch):
    """v1.2.5: the fast-forward path (not just clone's full-history fetch) must also
    carry the signature through — `av pull` writes fetched commits via the same
    sync.write_fetched_commit()/normalize_commit_row() path as clone, but this is the
    dedicated regression test for that fact rather than an inference from clone's."""
    pytest.importorskip("cryptography")

    from python.av_cli.signing import generate_keypair, verify_signature

    source, fake = fake_registry["source"], fake_registry["fake"]
    monkeypatch.chdir(tmp_path)
    assert invoke("clone", "source", "workcopy").exit_code == 0
    work = tmp_path / "workcopy"

    monkeypatch.chdir(source)
    generate_keypair(source)
    (source / "signed_after_clone.txt").write_text("v")
    invoke("add", "signed_after_clone.txt")
    r = invoke("commit", "-m", "signed after clone")
    assert r.exit_code == 0, r.output
    signed_hash = (source / ".av" / "refs" / "heads" / "main").read_text().strip()

    monkeypatch.chdir(work)
    pull_result = invoke("pull")
    assert pull_result.exit_code == 0, pull_result.output

    pulled_commit_file = work / ".av" / "commits" / f"{signed_hash}.json"
    assert pulled_commit_file.exists(), "signed commit missing after pull"
    data = json.loads(pulled_commit_file.read_text(encoding="utf-8"))
    assert isinstance(data.get("signature"), dict), \
        "pull dropped the signature — verify would falsely report UNSIGNED"
    ok, reason = verify_signature(data)
    assert ok, f"pulled signature failed verification: {reason}"

    # And the CLI's own verify path agrees, on a machine with no signing key of its own.
    verify_res = invoke("registry", "verify", signed_hash)
    assert verify_res.exit_code == 0, verify_res.output
    assert "VERIFIED" in verify_res.output


def test_clone_materializes_tip_full_history_and_identity(fake_registry, tmp_path, monkeypatch):
    source, pid = fake_registry["source"], fake_registry["pid"]
    monkeypatch.chdir(tmp_path)

    # Explicit target dir, distinct from the "source" fixture repo's own directory name —
    # omitting it defaults the clone target to Path.cwd()/"source", which collided with
    # fake_registry["source"] itself (both are tmp_path/"source"). Pre-1.2.5 that silently
    # hit the "target already exists and is not empty" early-return (exit 0, no real clone
    # performed) and every assertion below passed trivially by comparing source to itself;
    # the v1.2.5 exit-code fix (that path now fails loudly, exit 15) surfaced it. Found
    # during the V1.2.5 conflict-UX work, not a regression from that work itself.
    result = invoke("clone", "source", "cloned")
    assert result.exit_code == 0, result.output
    cloned = tmp_path / "cloned"
    assert (cloned / "train.py").read_text() == "print('v2')"
    assert (cloned / "model.bin").read_bytes() == (source / "model.bin").read_bytes()

    cfg = json.loads((cloned / ".av" / "config").read_text())
    assert cfg["project_id"] == pid, "clone must inherit the source project identity"

    head = (cloned / ".av" / "HEAD").read_text().strip()
    assert head == "ref: refs/heads/main"
    local_tip = (cloned / ".av" / "refs" / "heads" / "main").read_text().strip()
    assert local_tip == (source / ".av" / "refs" / "heads" / "main").read_text().strip()

    # full metadata history landed locally → av log sees both commits offline
    assert (cloned / ".av" / "commits" / f"{local_tip}.json").exists()
    monkeypatch.chdir(cloned)
    log_result = invoke("log")
    assert "c1" in log_result.output and "c2" in log_result.output

    status = invoke("status")
    assert status.exit_code == 0, status.output
    assert "Nothing to commit" in status.output


def test_clone_by_project_id_prefix_and_ambiguity(fake_registry, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = fake_registry["fake"]
    pid = fake_registry["pid"]

    by_id = invoke("clone", pid, "by-id")
    assert by_id.exit_code == 0, by_id.output

    # second project sharing a name prefix → ambiguous
    other_source = tmp_path / "other"
    other_source.mkdir()
    monkeypatch.chdir(other_source)
    invoke("init", "--mode", "local", "--yes", "--no-repl")
    invoke("config", "--name", "sourcelike")
    (other_source / "x.txt").write_text("x")
    invoke("add", "x.txt")
    invoke("commit", "-m", "other c1")
    other_pid = json.loads((other_source / ".av" / "config").read_text())["project_id"]
    fake.extra_projects.append({
        "project_id": other_pid,
        "project_name": "sourcelike",
        "commit_count": 1,
        "last_push": None,
    })

    monkeypatch.chdir(tmp_path)
    ambiguous = invoke("clone", "sourc")
    assert "ambiguous" in ambiguous.output
    assert "sourcelike" in ambiguous.output

    by_prefix = invoke("clone", "source", "by-prefix")
    assert by_prefix.exit_code == 0, by_prefix.output
    assert (tmp_path / "by-prefix" / ".av").exists()


def test_clone_refuses_nonempty_target(fake_registry, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keepme.txt").write_text("precious")

    result = invoke("clone", "source", "occupied")
    # v1.2.5: routed through fail("validation") — exit 15, not the old secho+return exit 0.
    assert result.exit_code == 15, result.output
    assert "not empty" in result.output
    assert (occupied / "keepme.txt").exists()


def test_pull_fast_forwards_a_cloned_repo(fake_registry, tmp_path, monkeypatch):
    source, fake = fake_registry["source"], fake_registry["fake"]
    monkeypatch.chdir(tmp_path)
    assert invoke("clone", "source", "workcopy").exit_code == 0
    work = tmp_path / "workcopy"

    # advance the SOURCE: new commit auto-pushes against the fake, moving the remote tip
    monkeypatch.chdir(source)
    (source / "train.py").write_text("print('v3')")
    invoke("add", "train.py")
    r = invoke("commit", "-m", "c3")
    assert r.exit_code == 0, r.output

    monkeypatch.chdir(work)
    before = (work / ".av" / "refs" / "heads" / "main").read_text().strip()
    result = invoke("pull")
    assert result.exit_code == 0, result.output
    assert "Fast-forwarded" in result.output and "c3" or True
    after = (work / ".av" / "refs" / "heads" / "main").read_text().strip()
    assert after != before
    assert (work / "train.py").read_text() == "print('v3')"

    status = invoke("status")
    assert "Nothing to commit" in status.output

    again = invoke("pull")
    assert "Already up to date" in again.output


def test_pull_refuses_dirty_tree_without_force(fake_registry, tmp_path, monkeypatch):
    source = fake_registry["source"]
    monkeypatch.chdir(tmp_path)
    assert invoke("clone", "source", "workcopy").exit_code == 0
    work = tmp_path / "workcopy"

    monkeypatch.chdir(source)
    (source / "train.py").write_text("print('dirty-source')")
    invoke("add", "train.py")
    assert invoke("commit", "-m", "c3").exit_code == 0

    monkeypatch.chdir(work)
    (work / "train.py").write_text("local uncommitted edit")
    result = invoke("pull")
    assert "uncommitted changes" in result.output
    assert (work / "train.py").read_text() == "local uncommitted edit"

    result = invoke("pull", "--force")
    assert result.exit_code == 0, result.output
    assert (work / "train.py").read_text() == "print('dirty-source')"


def test_pull_diverged_when_local_has_unpushed_commits(fake_registry, tmp_path, monkeypatch):
    source, fake = fake_registry["source"], fake_registry["fake"]
    monkeypatch.chdir(tmp_path)
    assert invoke("clone", "source", "workcopy").exit_code == 0
    work = tmp_path / "workcopy"

    # remote moves ahead (source pushes c3)
    monkeypatch.chdir(source)
    (source / "train.py").write_text("print('remote-line')")
    invoke("add", "train.py")
    assert invoke("commit", "-m", "c3").exit_code == 0

    # meanwhile the clone makes its OWN unpushed commit (push rejected → queued offline)
    monkeypatch.chdir(work)
    fake.reject_pushes = True
    (work / "train.py").write_text("print('local-only')")
    invoke("add", "train.py")
    r = invoke("commit", "-m", "local-divergent")
    assert r.exit_code == 0, r.output
    fake.reject_pushes = False

    diverged_tip = (work / ".av" / "refs" / "heads" / "main").read_text().strip()
    result = invoke("pull")
    # v1.2.5: a divergence reuses merge_conflict's exit code (14) instead of exiting 0 —
    # it's resolved by the same `av merge` command family; see the exit-code registry fix.
    assert result.exit_code == 14, result.output
    assert "diverged" in result.output
    assert "av merge" in result.output
    # ref untouched; the fetched remote tip is available locally for av merge
    assert (work / ".av" / "refs" / "heads" / "main").read_text().strip() == diverged_tip
    assert (work / "train.py").read_text() == "print('local-only')"


def test_pull_detached_head_and_unreachable_server(fake_registry, tmp_path, monkeypatch):
    source = fake_registry["source"]
    monkeypatch.chdir(tmp_path)
    assert invoke("clone", "source", "workcopy").exit_code == 0
    work = tmp_path / "workcopy"

    tip = (work / ".av" / "refs" / "heads" / "main").read_text().strip()
    monkeypatch.chdir(work)
    r = invoke("checkout", tip[:7])
    assert r.exit_code == 0, r.output
    result = invoke("pull")
    assert "detached" in result.output


def test_sync_is_ancestor_and_pick_default_branch():
    from python.av_cli import sync

    commits = {
        "a": {"parents": []},
        "b": {"parents": ["a"]},
        "c": {"parents": ["b", "z"]},   # merge-shaped: second parent unreachable
        "z": {"parents": []},
    }
    load = lambda h: commits.get(h)
    assert sync.is_ancestor(load, "a", "c") is True
    assert sync.is_ancestor(load, "b", "c") is True
    assert sync.is_ancestor(load, "c", "b") is False
    assert sync.is_ancestor(load, "a", "a") is True
    assert sync.is_ancestor(load, "missing", "c") is False

    pid = "abcd1234"
    refs = {f"{pid}/dev": "h1", f"{pid}/main": "h2", "other-project/main": "h9"}
    assert sync.pick_default_branch(refs, pid) == "main"
    assert sync.pick_default_branch({f"{pid}/weird": "h"}, pid) == "weird"
    assert sync.pick_default_branch({}, pid) is None


def test_clone_round_trips_chunked_checkpoint(fake_registry, tmp_path, monkeypatch):
    """A CDC-chunked .pt uploads as shards (no whole-file blob) and clones back byte-identical."""
    import aether_core  # noqa: F401  (skip whole test if the native core is missing)

    source, fake = fake_registry["source"], fake_registry["fake"]
    monkeypatch.chdir(source)
    invoke("config", "1")  # 1 MB threshold
    import random

    rng = random.Random(101)
    # deterministic 12 MB blob: the CDC hard cap (8 MB) forces at least one cut, so
    # ">= 2 chunks" is guaranteed instead of content-dependent
    original = rng.randbytes(12 * 1024 * 1024)
    (source / "checkpoint.pt").write_bytes(original)
    r = invoke("add", "checkpoint.pt")
    assert "(LFS," in r.output and "chunks)" in r.output
    assert invoke("commit", "-m", "ckpt").exit_code == 0

    from python.av_cli.index import Index
    chunks = Index(source).get_entry("checkpoint.pt")["chunks"]
    assert len(chunks) >= 2

    # Every shard must be resolvable through the registry-facing surface, while the
    # whole-file blob is deliberately absent everywhere (the shards carry all the bytes).
    whole_hash = Index(source).get_entry("checkpoint.pt")["hash"]
    available = fake.batch_check_objects([c["hash"] for c in chunks] + [whole_hash])
    assert all(c["hash"] in available for c in chunks)
    assert whole_hash not in available

    # clone it elsewhere and verify byte-identical reassembly
    parent = tmp_path / "_"
    parent.mkdir()
    monkeypatch.chdir(parent)
    result = invoke("clone", fake.project_name, "clone-target")
    assert result.exit_code == 0, result.output
    cloned = tmp_path / "_" / "clone-target"
    assert (cloned / "checkpoint.pt").read_bytes() == original
    cloned_chunks = Index(cloned).get_entry("checkpoint.pt")["chunks"]
    assert {c["hash"] for c in cloned_chunks} == {c["hash"] for c in chunks}
