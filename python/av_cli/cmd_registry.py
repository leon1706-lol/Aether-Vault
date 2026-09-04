"""av registry — backup/export & restore of a remote registry (v1.2.0 trust surface).

Export walks the registry's public API (commits, refs, runs, objects manifest) into a
portable archive directory; every object shard is downloaded and its hash RE-VERIFIED
during download, so an archive is self-validating. Restore re-ingests objects first,
then commits, then refs/runs (the same ordering the push path guarantees).

Attestation (integrity-v0): HMAC-SHA256 over the canonical commit JSON using a key from
.av/config (`av registry keygen`). Not asymmetric crypto and not a trust network — it
detects tampering by anyone without the key, which covers accidental corruption and
casual tampering. Asymmetric signing is tracked for the enterprise tier.
"""

import hashlib
import hmac as hmac_mod
import json
import pathlib  # NOT re-exported by `from .core import *` (core.py imports only the
                # `Path` class, never the module) — every pathlib.Path(...) call below
                # was a NameError on every real invocation until this import existed;
                # av registry export/restore had literally never worked (Probleme.md).

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


class _NullProgressBar:
    """Drop-in stand-in for click.progressbar() that just iterates — used in JSON mode
    so export/restore's item loop doesn't need two separate code paths."""

    def __init__(self, iterable):
        self._iterable = iterable

    def __enter__(self):
        return iter(self._iterable)

    def __exit__(self, *exc):
        return False


def ctx_exit(code):
    """Module-local exit helper (restore's failure path used to reference this name
    without defining it anywhere — a latent NameError on every failed restore)."""
    raise SystemExit(code)


@click.group()
def registry() -> None:
    """Registry-level operations: backup export/restore and commit attestation keys."""


def _state_path(out_path, kind: str) -> "pathlib.Path":
    # v1.3.0 fix (Probleme.md): export and restore each need their OWN "what's already
    # done" bookkeeping, even though both operate on the same ARCHIVE_DIR — export's
    # completed_objects means "already downloaded from the registry into this archive";
    # restore's means "already uploaded from this archive into the registry". A single
    # shared file used to let restore's own default --resume=True misread export's
    # bookkeeping as its own: the FIRST restore of a freshly-exported archive would see
    # every object already marked "completed" (by export, for a completely different
    # direction) and skip uploading all of them — silently a no-op restore into an empty
    # registry, exactly the disaster-recovery scenario this whole feature exists for.
    return out_path / f".{kind}-state.json"


def _load_state(out_path, kind: str) -> dict:
    p = _state_path(out_path, kind)
    if not p.exists():
        return {"completed_objects": [], "completed_commits": [], "completed_refs": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"completed_objects": [], "completed_commits": [], "completed_refs": []}


def _save_state(out_path, kind: str, state: dict) -> None:
    atomic_write_json(_state_path(out_path, kind), state)


@registry.command()
@click.argument("out_dir")
@click.option("--project", "project_id", default=None, help="Scope to one project.")
@click.option("--resume/--no-resume", default=True, show_default=True,
              help="Skip network I/O for objects/commits/refs a previous run into the "
                   "same OUT_DIR already completed (tracked in OUT_DIR/.export-state.json). "
                   "--no-resume forces a full pass regardless of prior state.")
def export(out_dir: str, project_id: str | None, resume: bool) -> None:
    """Export commits+refs+runs+objects from the configured registry into OUT_DIR."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    json_mode = current_output_mode() == "json"

    # v1.3.0: was an unhandled requests.exceptions.ConnectionError traceback on every
    # unreachable-server export — no other command in this codebase lets a raw
    # connection error escape uncaught.
    if not client.server_available():
        fail(None, "unreachable_queued", f"Registry unreachable at {client.server_url}.",
             command="registry export")

    out_path = pathlib.Path(out_dir)
    (out_path / "objects").mkdir(parents=True, exist_ok=True)
    manifest: dict = {"format": 1, "commits": [], "refs": [], "runs": [], "objects": []}
    state = _load_state(out_path, "export") if resume else {"completed_objects": [], "completed_commits": [], "completed_refs": []}
    done_objects = set(state["completed_objects"])

    def _get_json(path: str):
        import requests

        resp = client.session.get(f"{client.server_url}{path}", timeout=60)
        resp.raise_for_status()
        return resp.json()

    # commits (paged) — v1.3.0 fix (Probleme.md): include_layers=true is REQUIRED here.
    # Without it, GET /api/commits omits the "tree" key entirely (server.py::list_commits
    # only attaches it under that flag), so the object-discovery walk below silently found
    # zero hashes to export on every single real invocation this command has ever had —
    # a backup archive with commits/refs metadata but NO file content whatsoever.
    offset, limit = 0, 200
    while True:
        q = f"/api/commits?limit={limit}&offset={offset}&include_layers=true"
        if project_id:
            q += f"&project_id={project_id}"
        page = _get_json(q)
        rows = page.get("commits", [])
        manifest["commits"].extend(rows)
        if len(rows) < limit:
            break
        offset += limit

    refs = _get_json("/api/refs" + (f"?project_id={project_id}" if project_id else ""))
    manifest["refs"] = refs

    try:
        runs = _get_json(f"/api/runs?limit=1000" + (f"&project_id={project_id}" if project_id else ""))
        manifest["runs"] = runs.get("runs", [])
    except Exception:
        manifest["runs"] = []

    # unique object hashes referenced anywhere in the trees (+ layers/chunks):
    hashes: set[str] = set()

    def _walk(entry: dict):
        for info in entry.values():
            h = info.get("hash")
            if h:
                hashes.add(h)
            for layer in info.get("layers") or []:
                hashes.add(layer["hash"])
            for chunk in info.get("chunks") or []:
                hashes.add(chunk["hash"])

    for c in manifest["commits"]:
        _walk(c.get("tree") or {})

    ok = failed = skipped = 0
    sorted_hashes = sorted(hashes)
    # v1.3.0: a real progress bar over objects — the expensive part of an export.
    # Suppressed in JSON mode (an agent wants one clean envelope, not a progress bar
    # mixed into it) — same is_json check every other command in this codebase uses;
    # --silent has no ctx.obj flag to read here (it only gates logging setup today).
    with (click.progressbar(sorted_hashes, label="Downloading objects") if not json_mode
          else _NullProgressBar(sorted_hashes)) as bar:
        for h in bar:
            dest = out_path / "objects" / h[:2] / h[2:]
            this_ok = False
            if h in done_objects and dest.exists():
                ok += 1
                skipped += 1
                this_ok = True
            elif dest.exists():
                ok += 1
                this_ok = True
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    resp = client.session.get(f"{client.server_url}/api/objects/{h}", timeout=120)
                    resp.raise_for_status()
                    data = resp.content
                    if hashlib.sha256(data).hexdigest() != h:
                        raise ValueError("hash mismatch during download")
                    dest.write_bytes(data)
                    ok += 1
                    this_ok = True
                except Exception as exc:
                    failed += 1
                    if not json_mode:
                        click.secho(f"  object {h[:12]}… failed: {exc}", fg="yellow")
            manifest["objects"].append({"hash": h, "ok": this_ok})
            if this_ok:
                done_objects.add(h)
                state["completed_objects"] = sorted(done_objects)
                _save_state(out_path, "export", state)  # incremental — a killed export can resume

    (out_path / "manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")

    summary = {"dir": str(out_path), "commits": len(manifest["commits"]),
               "refs": len(manifest["refs"]), "runs": len(manifest["runs"]),
               "objects_ok": ok, "objects_failed": failed, "objects_resumed": skipped}
    if current_output_mode() == "json":
        emit_json(None, "registry export", data=summary)
        return
    click.secho(f"Exported to {out_path}: "
                f"{summary['commits']} commits, {summary['refs']} refs, "
                f"{summary['runs']} runs, {ok} objects"
                + (f", {failed} FAILED" if failed else ""), fg="green")


@registry.command()
@click.argument("archive_dir")
@click.option("--resume/--no-resume", default=True, show_default=True,
              help="Skip network I/O for objects/commits/refs a previous run against "
                   "this ARCHIVE_DIR already completed successfully (tracked in "
                   "ARCHIVE_DIR/.restore-state.json). --no-resume re-attempts everything — safe "
                   "either way since every write here is already idempotent (409/200 on "
                   "duplicate), just not free.")
def restore(archive_dir: str, resume: bool) -> None:
    """Re-ingest an `av registry export` archive into the configured registry.

    Ordering mirrors the push path (objects → commits → refs) and every object shard is
    hash-re-verified BEFORE upload, so a corrupted archive fails loudly instead of
    poisoning the registry. Duplicate hashes ingest as idempotent 409s — restoring into
    a partially-populated registry is safe; run `av gc` afterwards to sweep anything
    orphaned by the export.
    """
    repo_root = ensure_repo()
    client = _client(repo_root)
    json_mode = current_output_mode() == "json"
    if not client.server_available():
        fail(None, "unreachable_queued", f"Registry unreachable at {client.server_url}.",
             command="registry restore")
    src = pathlib.Path(archive_dir)
    manifest_path = src / "manifest.json"
    if not manifest_path.exists():
        fail(None, "validation", f"manifest.json not found in {src}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failed = 0
    state = _load_state(src, "restore") if resume else {"completed_objects": [], "completed_commits": [], "completed_refs": []}
    done_objects = set(state["completed_objects"])
    done_commits = set(state["completed_commits"])
    done_refs = set(state["completed_refs"])

    objects = manifest.get("objects", [])
    ok = dup = obj_resumed = 0
    with (click.progressbar(objects, label="Uploading objects") if not json_mode
          else _NullProgressBar(objects)) as bar:
        for entry in bar:
            h = entry["hash"]
            if h in done_objects:
                ok += 1
                obj_resumed += 1
                continue
            fpath = src / "objects" / h[:2] / h[2:]
            if not fpath.exists():
                failed += 1
                if not json_mode:
                    click.secho(f"  missing shard {h[:12]}… skipped", fg="yellow")
                continue
            data = fpath.read_bytes()
            if hashlib.sha256(data).hexdigest() != h:
                failed += 1
                if not json_mode:
                    click.secho(f"  CORRUPT archive shard {h[:12]}… skipped", fg="red")
                continue
            resp = client.session.post(f"{client.server_url}/api/objects/{h}", data=data,
                                       timeout=120)
            if resp.status_code in (201, 409):
                ok += 1
                if resp.status_code == 409:
                    dup += 1
                done_objects.add(h)
                state["completed_objects"] = sorted(done_objects)
                _save_state(src, "restore", state)
            else:
                failed += 1

    commits = manifest.get("commits", [])
    c_ok = c_dup = commit_resumed = 0
    with (click.progressbar(commits, label="Uploading commits  ") if not json_mode
          else _NullProgressBar(commits)) as bar:
        for commit in bar:
            h = commit.get("hash")
            if h and h in done_commits:
                c_ok += 1
                commit_resumed += 1
                continue
            payload = dict(commit)
            payload.pop("timestamp", None)  # server re-stamps from message payload if absent
            resp = client.session.post(f"{client.server_url}/api/commits", json=payload,
                                       timeout=60)
            if resp.status_code in (201, 409):
                c_ok += 1
                if resp.status_code == 409:
                    c_dup += 1
                if h:
                    done_commits.add(h)
                    state["completed_commits"] = sorted(done_commits)
                    _save_state(src, "restore", state)
            else:
                failed += 1

    r_ok = 0
    refs = manifest.get("refs") or {}
    for name, ref_hash in refs.items():
        if name in done_refs:
            r_ok += 1
            continue
        resp = client.session.put(f"{client.server_url}/api/refs/{name}",
                                  json={"commit_hash": ref_hash}, timeout=60)
        if resp.status_code == 200:
            r_ok += 1
            done_refs.add(name)
            state["completed_refs"] = sorted(done_refs)
            _save_state(src, "restore", state)
        else:
            failed += 1

    summary = {"objects_uploaded": ok, "objects_duplicate": dup, "objects_resumed": obj_resumed,
               "commits": c_ok, "commits_duplicate": c_dup, "commits_resumed": commit_resumed,
               "refs": r_ok, "failed": failed}
    if current_output_mode() == "json":
        emit_json(None, "registry restore", data=summary)
        return
    if failed:
        click.secho(f"Restore INCOMPLETE: {summary}", fg="red")
        ctx_exit(EXIT_VALIDATION)
    click.secho(f"Restored: {summary}", fg="green")


@registry.command("keygen")
def keygen() -> None:
    """Generate an ed25519 signing keypair under .av/keys/.

    v1.2.2: commits are then AUTO-SIGNED at commit time (signature over the canonical
    sorted-keys JSON of the payload minus the signature itself) and validated by
    `av verify <hash>`. Trust model — tamper evidence, not a trust network (SECURITY.md):
    the embedded public key proves payload integrity, not owner identity. Requires the
    [sign] extra (`pip install aether-vault[sign]`)."""
    from .signing import SigningUnavailable, generate_keypair

    repo_root = ensure_repo()
    try:
        priv, pub = generate_keypair(repo_root)
    except SigningUnavailable as exc:
        fail(None, "validation", str(exc))
    except FileExistsError:
        fail(None, "validation",
             "Signing keys already exist in .av/keys/ — delete them first to rotate.")
    if current_output_mode() == "json":
        emit_json(None, "registry keygen", data={"configured": True,
                                                 "algo": "ed25519",
                                                 "private_key": str(priv),
                                                 "public_key": str(pub)})
        return
    click.secho("ed25519 signing keypair generated.", fg="green")
    click.secho(f"  private: {priv} (0600)", fg="cyan")
    click.secho(f"  public:  {pub}", fg="cyan")
    click.secho("Commits are now signed automatically; verify with `av verify <hash>`.",
                fg="cyan")


@registry.group("keys")
def keys_group() -> None:
    """v1.2.5: signing-key management — list, fingerprint, rotate.

    Tamper evidence, not a PKI: none of these commands bind a key to an identity — they
    only manage which bytes THIS repo signs with next."""


@keys_group.command("list")
def keys_list() -> None:
    """List every signing key this repo knows about (active + archived from past rotations).

    Tamper evidence, not PKI / identity binding — see `av registry keys --help`."""
    from .signing import list_keys

    repo_root = ensure_repo()
    entries = list_keys(repo_root)
    if current_output_mode() == "json":
        emit_json(None, "registry keys list", data={"keys": entries})
        return
    if not entries:
        click.secho("No signing keys — run `av registry keygen` first.", fg="yellow")
        return
    for e in entries:
        state = "active" if e["active"] else "archived"
        click.echo(f"  [{state}] {e['fingerprint']}  (created {e['created_at']})")


@keys_group.command("fingerprint")
def keys_fingerprint() -> None:
    """Print just this repo's active-key fingerprint (scriptable).

    Tamper evidence, not PKI / identity binding — see `av registry keys --help`."""
    from .signing import fingerprint, public_key_path

    repo_root = ensure_repo()
    pub_path = public_key_path(repo_root)
    if not pub_path.exists():
        fail(None, "validation", "No signing key — run `av registry keygen` first.")
    fp = fingerprint(pub_path.read_bytes())
    if current_output_mode() == "json":
        emit_json(None, "registry keys fingerprint", data={"fingerprint": fp})
        return
    click.echo(fp)


@keys_group.command("rotate")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip the confirmation prompt.")
def keys_rotate(yes: bool) -> None:
    """Archive the current signing key and generate a fresh one.

    Tamper evidence, not PKI / identity binding: old commits keep verifying (their
    signature carries the OLD public key embedded), new commits sign with the new key.
    The archived private key is never deleted."""
    from .signing import fingerprint, public_key_path, rotate_keypair

    repo_root = ensure_repo()
    pub_path = public_key_path(repo_root)
    old_fp = fingerprint(pub_path.read_bytes()) if pub_path.exists() else None

    if not yes and current_output_mode() != "json":
        label = f"key {old_fp}" if old_fp else "the current key"
        if not click.confirm(f"Archive {label} and generate a new signing key?", default=False):
            click.secho("Aborted — nothing rotated.", fg="yellow")
            return

    try:
        priv, pub = rotate_keypair(repo_root)
    except FileNotFoundError as exc:
        fail(None, "validation", str(exc))
    new_fp = fingerprint(pub.read_bytes())
    if current_output_mode() == "json":
        emit_json(None, "registry keys rotate", data={
            "archived_fingerprint": old_fp, "new_fingerprint": new_fp,
            "private_key": str(priv), "public_key": str(pub),
        })
        return
    click.secho(f"Rotated: {old_fp or '(none)'} → {new_fp}", fg="green")
    click.secho("Old commits still verify (their signature embeds the old public key); "
               "new commits sign with the new key.", fg="cyan")


@registry.command("export-signature")
@click.argument("commit_hash")
@click.option("--out", "out_path", default=None, type=click.Path(dir_okay=False),
              help="Write to this file instead of stdout.")
def export_signature(commit_hash: str, out_path: str | None) -> None:
    """v1.2.5: export COMMIT_HASH's signature as a standalone record for external audit
    — verify it elsewhere with `av registry verify <hash> --signature FILE`, without that
    verifier needing this repo's config or registry access.

    Tamper evidence, not PKI / identity binding: this proves the exported record matches
    what the signer produced, not who the signer is."""
    from .handoff import load_commit
    from .signing import export_signature_blob

    repo_root = ensure_repo()
    commit = load_commit(repo_root, commit_hash)
    if not commit:
        fail(None, "validation", f"Unknown commit: {commit_hash}")
    try:
        blob = export_signature_blob(commit_hash, commit)
    except ValueError as exc:
        fail(None, "validation", str(exc))

    rendered = json.dumps(blob, indent=2, sort_keys=True)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(rendered + "\n")
        if current_output_mode() == "json":
            emit_json(None, "registry export-signature", data={**blob, "path": out_path})
        else:
            click.secho(f"Wrote signature record to {out_path}", fg="green")
        return
    if current_output_mode() == "json":
        emit_json(None, "registry export-signature", data=blob)
        return
    click.echo(rendered)


@registry.command()
@click.argument("commit_hash")
def attest(commit_hash: str) -> None:
    """Attach an HMAC attestation tag to COMMIT_HASH (key required)."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    key = cfg.get("attest_key")
    if not key:
        fail(None, "validation", "No attestation key — run `av registry keygen` first.")
    from .handoff import load_commit

    commit = load_commit(repo_root, commit_hash)
    if not commit:
        fail(None, "validation", f"Unknown commit: {commit_hash}")
    sig = hmac_mod.new(key.encode(), commit_hash.encode(), hashlib.sha256).hexdigest()
    tag = f"attest:{sig[:16]}"
    result = invoke_mergeless_attest_tag(repo_root, [tag])
    if current_output_mode() == "json":
        emit_json(None, "registry attest", data={"commit": commit_hash,
                                                 "signature": sig,
                                                 "applied_via_new_commit": result})
        return
    click.secho(f"Attestation signature {sig[:16]}… recorded via metadata commit.", fg="green")


def invoke_mergeless_attest_tag(repo_root, tags):
    """Records the attestation as a zero-content metadata commit (audit-friendly)."""
    from .index import Index
    from .core import commit_staged

    idx = Index(repo_root)
    if not idx.get_staged_entries():
        # nothing staged: create a marker file so the metadata commit has content
        marker = repo_root / ".av" / "attestations.log"
        with open(marker, "a", encoding="utf-8") as f:
            f.write(json.dumps({"tagged_at": datetime.datetime.now(
                datetime.timezone.utc).isoformat(), "tags": tags}) + "\n")
    return commit_staged(repo_root, "attestation record", tags=tuple(tags), defer_upload=True)


@registry.command()
@click.argument("commit_hash")
@click.option("--signature", "signature_file", default=None, type=click.Path(exists=True, dir_okay=False),
              help="v1.2.5: verify against a detached signature record (from "
                   "`av registry export-signature`) instead of the commit's own embedded "
                   "signature — for auditing a commit without this repo's config.")
def verify(commit_hash: str, signature_file: str | None) -> None:
    """Verify COMMIT_HASH: an ed25519 commit signature when present, else a legacy
    HMAC attestation tag.

    Tamper evidence, not PKI / identity binding — this proves the payload wasn't
    modified after signing, not who the signer is (see SECURITY.md).

    v1.2.2 verification order:
    1. `signature` blob on the commit → validate the ed25519 signature over the
       canonical (sorted-keys, signature-stripped) payload. Works on any clone — the
       signature and public key ride the commit itself (server persists them).
    2. Legacy `attest:<prefix>` tag → HMAC check against this repo's attest_key.
    3. Neither → UNSIGNED (exit 0 in text mode; unsigned commits are valid — this is
       tamper EVIDENCE, not a trust gate)."""
    from .handoff import load_commit
    from .signing import SigningUnavailable, load_public_key_hex, verify_detached, verify_signature

    repo_root = ensure_repo()

    commit = load_commit(repo_root, commit_hash)
    if not commit:
        fail(None, "validation", f"Unknown commit: {commit_hash}")

    if signature_file:
        with open(signature_file, "r", encoding="utf-8") as f:
            detached = json.load(f)
        try:
            ok, reason = verify_detached(commit, detached)
        except SigningUnavailable as exc:
            fail(None, "validation", str(exc))
        data = {"verified": ok, "reason": reason, "scheme": "ed25519-detached",
               "detached_fingerprint": detached.get("fingerprint")}
        if current_output_mode() == "json":
            emit_json(None, "registry verify", data=data)
            return
        if ok:
            click.secho(f"VERIFIED (detached record, {detached.get('fingerprint')})", fg="green")
        else:
            click.secho(f"TAMPERED OR INVALID: {reason}", fg="red")
            ctx_exit(EXIT_VALIDATION)
        return

    if isinstance(commit.get("signature"), dict):
        try:
            ok, reason = verify_signature(commit)
        except SigningUnavailable as exc:
            fail(None, "validation", str(exc))
        local_key = load_public_key_hex(repo_root)
        matches_local_key = bool(local_key) and \
            commit["signature"].get("public_key") == local_key
        data = {"verified": ok, "reason": reason, "scheme": "ed25519",
                "signed_with_this_repos_key": matches_local_key,
                "signature_prefix": str(commit["signature"].get("sig", ""))[:16]}
        if current_output_mode() == "json":
            emit_json(None, "registry verify", data=data)
            return
        if ok:
            click.secho(f"VERIFIED ({'this repo key' if matches_local_key else 'embedded key'})", fg="green")
        else:
            click.secho(f"TAMPERED OR INVALID: {reason}", fg="red")
            ctx_exit(EXIT_VALIDATION)
        return

    # Legacy attestation path (integrity-v0, kept for existing repos).
    cfg = load_config(repo_root)
    key = cfg.get("attest_key")
    if not key:
        if current_output_mode() == "json":
            emit_json(None, "registry verify", data={"verified": False,
                                                     "reason": "unsigned",
                                                     "scheme": None})
            return
        click.secho("UNSIGNED — no commit signature and no attestation key here.", fg="yellow")
        return
    expected = hmac_mod.new(key.encode(), commit["hash"].encode(),
                            hashlib.sha256).hexdigest()
    tags = commit.get("tags") or []
    match = any(t == f"attest:{expected[:16]}" for t in tags)
    if current_output_mode() == "json":
        emit_json(None, "registry verify", data={"verified": match,
                                                 "scheme": "hmac-attest",
                                                 "signature_prefix": expected[:16]})
        return
    click.secho("VERIFIED" if match else "NO MATCHING ATTESTATION",
                fg="green" if match else "red")
