"""av review / av critique — the reviewer gate + structured objections (v1.3.1, RSI R4:
todo.md H.34/H.35).

A review targets either a change set or an improver version (`--target-type`); the
server rejects a self-review (the target's own proposer) and requires the `review`
scope. `av improver promote`'s `require_review` gate checks
`GET /api/reviews?target_type=improver&target_id=<candidate>` directly.
"""
from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import EXIT_VALIDATION, current_output_mode, emit_json, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


def _require_online(repo_root):
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued",
             f"Registry unreachable at {client.server_url} — reviews are server-authoritative.")
    return client


_TARGET_TYPES = ("change_set", "improver")


@click.group()
def review() -> None:
    """The reviewer gate: approve/reject a change set or an improver version."""


def _submit(target_type: str, target_id: str, decision: str, comment: str | None) -> None:
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.post(f"{client.server_url}/api/reviews", json={
        "target_type": target_type, "target_id": target_id, "decision": decision,
        "comment": comment,
    })
    if resp.status_code == 403:
        fail(None, "scope_denied", "Token lacks the 'review' scope.")
    if resp.status_code == 422 and "own proposer" in resp.text:
        fail(None, "validation", "You proposed this — another identity must review it.")
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Registry rejected the review: {resp.text[:200]}")
    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "review", data=body)
        return
    click.secho(f"Review recorded: {decision} ({body['id'][:8]})",
               fg="green" if decision == "approve" else "yellow")


@review.command("approve")
@click.argument("target_id")
@click.option("--target-type", type=click.Choice(_TARGET_TYPES), default="improver", show_default=True)
@click.option("--comment", default=None)
def review_approve(target_id: str, target_type: str, comment: str | None) -> None:
    _submit(target_type, target_id, "approve", comment)


@review.command("reject")
@click.argument("target_id")
@click.option("--target-type", type=click.Choice(_TARGET_TYPES), default="improver", show_default=True)
@click.option("--comment", default=None)
def review_reject(target_id: str, target_type: str, comment: str | None) -> None:
    _submit(target_type, target_id, "reject", comment)


@review.command("list")
@click.option("--target-type", type=click.Choice(_TARGET_TYPES), default=None)
@click.option("--target", "target_id", default=None)
def review_list(target_type: str | None, target_id: str | None) -> None:
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    params = {}
    if target_type:
        params["target_type"] = target_type
    if target_id:
        params["target_id"] = target_id
    resp = client.session.get(f"{client.server_url}/api/reviews", params=params)
    rows = resp.json().get("reviews", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "review list", data={"reviews": rows})
        return
    for r in rows:
        click.echo(f"  [{r['decision']:>7}] {r['target_type']}:{r['target_id'][:8]} "
                  f"by {r.get('reviewer') or '-'}")


# ---------------------------------------------------------------------------
# Critiques (todo.md H.35) — structured objections on a change set; must be resolved
# or explicitly waived before promotion clears.
# ---------------------------------------------------------------------------

@click.group()
def critique() -> None:
    """Structured objections attached to a change set or an improver version."""


@critique.command("add")
@click.argument("target_id")
@click.argument("objection")
@click.option("--target-type", type=click.Choice(_TARGET_TYPES), default="improver", show_default=True)
def critique_add(target_id: str, objection: str, target_type: str) -> None:
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.post(f"{client.server_url}/api/critiques",
                               json={"target_type": target_type, "target_id": target_id,
                                     "objection": objection})
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Could not raise critique: {resp.text[:200]}")
    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "critique add", data=body)
        return
    click.secho(f"Critique {body['id'][:8]} raised on {target_type}:{target_id}.", fg="yellow")


@critique.command("list")
@click.option("--target-type", type=click.Choice(_TARGET_TYPES), default=None)
@click.option("--target", "target_id", default=None)
@click.option("--status", type=click.Choice(["open", "resolved", "waived"]), default=None)
def critique_list(target_type: str | None, target_id: str | None, status: str | None) -> None:
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    params = {}
    if target_type:
        params["target_type"] = target_type
    if target_id:
        params["target_id"] = target_id
    if status:
        params["status"] = status
    resp = client.session.get(f"{client.server_url}/api/critiques", params=params)
    rows = resp.json().get("critiques", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "critique list", data={"critiques": rows})
        return
    for r in rows:
        click.echo(f"  [{r['status']:>8}] {r['id'][:8]} ({r['target_type']}:{r['target_id'][:8]}): {r['objection']}")


def _finalize_critique(critique_id: str, action: str, resolution: str | None) -> dict:
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.post(f"{client.server_url}/api/critiques/{critique_id}/{action}",
                               json={"resolution": resolution})
    if resp.status_code == 403:
        fail(None, "scope_denied", f"Token lacks the 'review' scope required to {action} a critique.")
    if resp.status_code == 409:
        fail(None, "validation", f"Critique {critique_id} is already finalized.")
    if resp.status_code != 200:
        fail(None, "validation", f"Could not {action} critique: {resp.text[:200]}")
    return resp.json()


@critique.command("resolve")
@click.argument("critique_id")
@click.option("--resolution", default=None, help="How the objection was addressed.")
def critique_resolve(critique_id: str, resolution: str | None) -> None:
    body = _finalize_critique(critique_id, "resolve", resolution)
    if current_output_mode() == "json":
        emit_json(None, "critique resolve", data=body)
        return
    click.secho(f"Critique {critique_id} resolved.", fg="green")


@critique.command("waive")
@click.argument("critique_id")
@click.option("--resolution", required=True, help="Why the objection is being overridden.")
def critique_waive(critique_id: str, resolution: str) -> None:
    """Waiving (unlike resolving) means the objection STANDS but is deliberately
    overridden — requires the `review` scope, always audited."""
    body = _finalize_critique(critique_id, "waive", resolution)
    if current_output_mode() == "json":
        emit_json(None, "critique waive", data=body)
        return
    click.secho(f"Critique {critique_id} waived: {resolution}", fg="yellow")
