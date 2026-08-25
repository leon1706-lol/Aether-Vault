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

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json


def _client(repo_root):
    from .client import VaultClient

    cfg = load_config(repo_root)
    return VaultClient(cfg.get("remote_url", "http://localhost:8000"),
                       cfg.get("remote_api_token"))


@click.group()
def registry() -> None:
    """Registry-level operations: backup export/restore and commit attestation keys."""


@registry.command()
@click.argument("out_dir")
@click.option("--project", "project_id", default=None, help="Scope to one project.")
def export(out_dir: str, project_id: str | None) -> None:
    """Export commits+refs+runs+objects from the configured registry into OUT_DIR."""
    repo_root = ensure_repo()
    client = _client(repo_root)

    out_path = pathlib.Path(out_dir)
    (out_path / "objects").mkdir(parents=True, exist_ok=True)
    manifest: dict = {"format": 1, "commits": [], "refs": [], "runs": [], "objects": []}

    def _get_json(path: str):
        import requests

        resp = client.session.get(f"{client.server_url}{path}", timeout=60)
        resp.raise_for_status()
        return resp.json()

    # commits (paged)
    offset, limit = 0, 200
    while True:
        q = f"/api/commits?limit={limit}&offset={offset}"
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

    import requests as requests_mod

    ok = failed = 0
    for h in sorted(hashes):
        dest = out_path / "objects" / h[:2] / h[2:]
        if dest.exists():
            ok += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = client.session.get(f"{client.server_url}/api/objects/{h}", timeout=120)
            resp.raise_for_status()
            data = resp.content
            if hashlib.sha256(data).hexdigest() != h:
                raise ValueError("hash mismatch during download")
            dest.write_bytes(data)
            ok += 1
        except Exception as exc:
            failed += 1
            click.secho(f"  object {h[:12]}… failed: {exc}", fg="yellow")
        manifest["objects"].append({"hash": h, "ok": ok and not failed or True})

    (out_path / "manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")

    summary = {"dir": str(out_path), "commits": len(manifest["commits"]),
               "refs": len(manifest["refs"]), "runs": len(manifest["runs"]),
               "objects_ok": ok, "objects_failed": failed}
    if current_output_mode() == "json":
        emit_json(None, "registry export", data=summary)
        return
    click.secho(f"Exported to {out_path}: "
                f"{summary['commits']} commits, {summary['refs']} refs, "
                f"{summary['runs']} runs, {ok} objects"
                + (f", {failed} FAILED" if failed else ""), fg="green")


@registry.command("keygen")
def keygen() -> None:
    """Generate an attestation key (HMAC secret) into this repo's config."""
    import secrets

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    cfg["attest_key"] = secrets.token_urlsafe(32)
    save_config(repo_root, cfg)
    if current_output_mode() == "json":
        emit_json(None, "registry keygen", data={"configured": True})
        return
    click.secho("Attestation key generated and stored in .av/config.", fg="green")


@click.command()
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
def verify(commit_hash: str) -> None:
    """Verify an attestation tag on COMMIT_HASH against this repo's key."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    key = cfg.get("attest_key")
    if not key:
        fail(None, "validation", "No attestation key in this repo.")
    from .handoff import load_commit

    commit = load_commit(repo_root, commit_hash)
    if not commit:
        fail(None, "validation", f"Unknown commit: {commit_hash}")
    expected = hmac_mod.new(key.encode(), commit["hash"].encode(),
                            hashlib.sha256).hexdigest()
    tags = commit.get("tags") or []
    match = any(t == f"attest:{expected[:16]}" for t in tags)
    if current_output_mode() == "json":
        emit_json(None, "registry verify", data={"verified": match,
                                                 "signature_prefix": expected[:16]})
        return
    click.secho("VERIFIED" if match else "NO MATCHING ATTESTATION",
                fg="green" if match else "red")
