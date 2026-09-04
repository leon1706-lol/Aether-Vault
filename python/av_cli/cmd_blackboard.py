"""av blackboard — durable shared claims with evidence links (v1.3.1, RSI R4:
todo.md H.36). Beyond the ordered event stream: a place for hypotheses that outlive any
one event, with authors and evidence, resolvable once settled.
"""
import json

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


def _require_online(repo_root):
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued",
             f"Registry unreachable at {client.server_url} — the blackboard is server-authoritative.")
    return client


@click.group()
def blackboard() -> None:
    """Durable shared claims with authors and evidence links."""


@blackboard.command("post")
@click.argument("claim")
@click.option("--evidence", "evidence_refs", multiple=True,
              help='"type:ref", e.g. "run:r123" or "commit:abcd1234" (repeatable).')
def blackboard_post(claim: str, evidence_refs: tuple) -> None:
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    evidence = []
    for raw in evidence_refs:
        kind, sep, ref = raw.partition(":")
        if not sep:
            fail(None, "validation", f'Malformed --evidence {raw!r} — expected "type:ref".')
        evidence.append({"type": kind, "ref": ref})
    resp = client.session.post(f"{client.server_url}/api/blackboard", json={
        "project_id": cfg["project_id"], "claim": claim, "evidence": evidence,
    })
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Registry rejected the claim: {resp.text[:200]}")
    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "blackboard post", data=body)
        return
    click.secho(f"Claim {body['id'][:8]} posted.", fg="green")


@blackboard.command("list")
@click.option("--status", type=click.Choice(["open", "resolved"]), default=None)
@click.option("--project", "project_id", default=None)
def blackboard_list(status: str | None, project_id: str | None) -> None:
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    params = {"project_id": project_id or cfg.get("project_id")}
    if status:
        params["status"] = status
    resp = client.session.get(f"{client.server_url}/api/blackboard", params=params)
    rows = resp.json().get("entries", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "blackboard list", data={"entries": rows})
        return
    for r in rows:
        click.echo(f"  [{r['status']:>8}] {r['id'][:8]}: {r['claim']} "
                  f"({len(r['evidence'])} evidence)")


@blackboard.command("resolve")
@click.argument("entry_id")
def blackboard_resolve(entry_id: str) -> None:
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.post(f"{client.server_url}/api/blackboard/{entry_id}/resolve")
    if resp.status_code != 200:
        fail(None, "validation", f"Could not resolve {entry_id}: {resp.text[:200]}")
    if current_output_mode() == "json":
        emit_json(None, "blackboard resolve", data=resp.json())
        return
    click.secho(f"Claim {entry_id} resolved.", fg="green")
