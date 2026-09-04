"""av lessons — the distilled "what we believe now" document (v1.3.1, RSI R4:
todo.md E.23). Content-addressed and versioned like every other RSI artifact, but
revises freely (no hash-chain) — `av lessons update` publishes a new version; `show`
resolves the latest by default.
"""
import datetime
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
             f"Registry unreachable at {client.server_url} — lessons are server-authoritative.")
    return client


@click.group()
def lessons() -> None:
    """The distilled, versioned "what we believe now" document."""


@lessons.command("update")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
def lessons_update(file: str) -> None:
    """Publish FILE as the new current lessons version."""
    from . import casobj

    repo_root = ensure_repo()
    client = _require_online(repo_root)
    doc = json.loads(Path(file).read_text(encoding="utf-8"))
    doc.setdefault("updated_at", datetime.datetime.now(datetime.timezone.utc).isoformat())
    object_id = casobj.write_object(repo_root, doc)
    if not client.upload_object(casobj.object_path(repo_root, object_id), object_id):
        fail(None, "unreachable_queued", "Failed to upload the lessons object.")

    cfg = load_config(repo_root)
    resp = client.session.post(f"{client.server_url}/api/lessons",
                               json={"project_id": cfg["project_id"], "object_id": object_id})
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Registry rejected the lessons update: {resp.text[:200]}")
    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "lessons update", data={**body, "object_id": object_id})
        return
    click.secho(f"Lessons updated ({body['id'][:8]}).", fg="green")


@lessons.command("show")
@click.option("--project", "project_id", default=None)
def lessons_show(project_id: str | None) -> None:
    """Show the CURRENT (latest) lessons document."""
    from . import casobj

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/lessons/latest",
                              params={"project_id": project_id or cfg.get("project_id")})
    if resp.status_code != 200:
        if current_output_mode() == "json":
            emit_json(None, "lessons show", data=None)
            return
        click.secho("No lessons published for this project yet.", fg="yellow")
        return
    row = resp.json()
    doc = casobj.read_object(repo_root, row["object_id"])
    if doc is None and client.download_object(row["object_id"],
                                              casobj.object_path(repo_root, row["object_id"])):
        doc = casobj.read_object(repo_root, row["object_id"])
    if current_output_mode() == "json":
        emit_json(None, "lessons show", data={**row, "document": doc})
        return
    click.echo(json.dumps(doc, indent=2))
