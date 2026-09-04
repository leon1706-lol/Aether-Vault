"""av tools manifest — per-improver-version tool permission manifests (v1.3.1, RSI R5:
todo.md G.30). Local-first (`.av/tool_manifests/<improver_id>.json`, what
`av sandbox run --improver` actually enforces against) with an optional publish step to
the registry for a durable, shared version history.
"""
import json

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


@click.group()
def tools() -> None:
    """Per-improver-version tool permission manifests."""


@tools.group("manifest")
def tools_manifest() -> None:
    """writable paths, network policy, GPU access — enforced by `av sandbox run --improver`."""


@tools_manifest.command("show")
@click.argument("improver_id")
def manifest_show(improver_id: str) -> None:
    from .sandbox.manifest import load_manifest

    repo_root = ensure_repo()
    manifest = load_manifest(repo_root, improver_id)
    if current_output_mode() == "json":
        emit_json(None, "tools manifest show", data=manifest)
        return
    click.echo(json.dumps(manifest, indent=2))


@tools_manifest.command("set")
@click.argument("improver_id")
@click.option("--writable-path", "writable_paths", multiple=True,
              help="Glob a sandboxed job may write to (repeatable).")
@click.option("--network", type=click.Choice(["none", "bridge"]), default=None)
@click.option("--network-destination", "network_destinations", multiple=True,
              help="Declared (audit-only — see module docstring) network destination.")
@click.option("--gpu/--no-gpu", default=None)
@click.option("--publish", is_flag=True, default=False,
              help="Also publish this manifest to the registry as a durable version.")
def manifest_set(improver_id: str, writable_paths: tuple, network: str | None,
                 network_destinations: tuple, gpu: bool | None, publish: bool) -> None:
    from .sandbox.manifest import load_manifest, save_manifest

    repo_root = ensure_repo()
    manifest = dict(load_manifest(repo_root, improver_id))
    if writable_paths:
        manifest["writable_paths"] = list(writable_paths)
    if network is not None:
        manifest["network"] = network
    if network_destinations:
        manifest["network_destinations"] = list(network_destinations)
    if gpu is not None:
        manifest["gpu"] = gpu
    save_manifest(repo_root, improver_id, manifest)

    published_id = None
    if publish:
        from . import casobj

        client = _client(repo_root)
        if not client.server_available():
            fail(None, "unreachable_queued", "Registry unreachable — cannot publish (saved locally).")
        object_id = casobj.write_object(repo_root, manifest)
        if not client.upload_object(casobj.object_path(repo_root, object_id), object_id):
            fail(None, "unreachable_queued", "Failed to upload the manifest object.")
        cfg = load_config(repo_root)
        resp = client.session.post(f"{client.server_url}/api/tool-manifests", json={
            "project_id": cfg["project_id"], "improver_id": improver_id, "object_id": object_id,
        })
        if resp.status_code not in (200, 201):
            fail(None, "validation", f"Registry rejected the manifest: {resp.text[:200]}")
        published_id = resp.json().get("id")

    if current_output_mode() == "json":
        emit_json(None, "tools manifest set", data={"improver_id": improver_id,
                                                     "manifest": manifest, "published_id": published_id})
        return
    click.secho(f"Manifest for {improver_id} saved." + (f" Published ({published_id[:8]})." if published_id else ""),
               fg="green")


@tools_manifest.command("verify")
@click.argument("improver_id")
@click.argument("command", nargs=-1, required=True)
@click.option("--mount", "mounts_raw", multiple=True, help='"host:container[:ro|rw]" (repeatable).')
@click.option("--network", type=click.Choice(["none", "bridge"]), default="none")
@click.option("--gpu", is_flag=True, default=False)
def manifest_verify(improver_id: str, command: tuple, mounts_raw: tuple, network: str, gpu: bool) -> None:
    """Dry-run check: would `av sandbox run --improver IMPROVER_ID` be allowed to run
    COMMAND with these mounts/network/gpu? Touches nothing either way."""
    from .cmd_sandbox import _parse_mount
    from .sandbox.base import JobSpec
    from .sandbox.manifest import load_manifest, verify_spec_against_manifest

    repo_root = ensure_repo()
    manifest = load_manifest(repo_root, improver_id)
    spec = JobSpec(job_id="verify-only", command=list(command),
                   mounts=[_parse_mount(m) for m in mounts_raw], network=network, gpu=gpu)
    ok, reason = verify_spec_against_manifest(spec, manifest)
    if current_output_mode() == "json":
        emit_json(None, "tools manifest verify", data={"allowed": ok, "reason": reason})
        return
    click.secho(f"{'ALLOWED' if ok else 'DENIED'}: {reason}", fg="green" if ok else "red")
    if not ok:
        raise SystemExit(EXIT_VALIDATION)
