"""av freeze — global per-project kill-switch (v1.3.1). Freeze blocks PROMOTIONS and
SELF-EDITS specifically (`av promote`, `av improver register/propose/apply`, `av policy
pack publish`), never ordinary training commits — `av improver rollback` and `av freeze
off` are exempt by construction, so an incident can never lock you out of undoing it.
Server-side, unfreezing requires the `admin` scope, so a rogue local client can't bypass
this by skipping `freeze_guard()` locally.
"""
from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


def project_frozen(repo_root) -> tuple[bool, str | None]:
    """(frozen, reason) for this repo's project — best-effort: an unreachable registry
    or a repo with no config yet is treated as NOT frozen (freeze is an explicit, online,
    opt-in gate; it must never itself become a new offline-resilience hazard)."""
    try:
        cfg = load_config(repo_root)
        client = _client(repo_root)
        if not client.server_available():
            return False, None
        resp = client.session.get(f"{client.server_url}/api/freeze/{cfg['project_id']}", timeout=5)
        if resp.status_code != 200:
            return False, None
        body = resp.json()
        return bool(body.get("frozen")), body.get("reason")
    except Exception:
        return False, None


def freeze_guard(repo_root) -> None:
    """Called explicitly at the top of every promotion/self-edit gate command (`av
    promote`, `av improver register/propose/apply`, `av policy pack publish`) — fails
    fast with exit 18 when the project is frozen. A no-op when not frozen, offline, or
    outside a repo (fail-open by design — see `project_frozen()`)."""
    frozen, reason = project_frozen(repo_root)
    if frozen:
        fail(None, "frozen",
             f"Project is frozen ({reason or 'no reason given'}) — promotions and self-"
             "edits are paused. `av improver rollback` and `av freeze off` still work. "
             "See `av freeze status`.")


@click.group(invoke_without_command=True)
@click.pass_context
def freeze(ctx) -> None:
    """Global per-project kill-switch: freeze to stop all writes except rollback."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(freeze_status)


@freeze.command("status")
def freeze_status() -> None:
    """Show whether this project is frozen."""
    repo_root = ensure_repo()
    frozen, reason = project_frozen(repo_root)
    if current_output_mode() == "json":
        emit_json(None, "freeze status", data={"frozen": frozen, "reason": reason})
        return
    if frozen:
        click.secho(f"FROZEN — {reason or 'no reason given'}", fg="red", bold=True)
    else:
        click.secho("Not frozen.", fg="green")


def _set_freeze(repo_root, frozen: bool, reason: str | None) -> dict:
    cfg = load_config(repo_root)
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued",
             f"Registry unreachable at {client.server_url} — freeze state is server-"
             "authoritative and cannot be set locally-only.")
    resp = client.session.post(f"{client.server_url}/api/freeze/{cfg['project_id']}",
                               json={"frozen": frozen, "reason": reason})
    if resp.status_code == 403:
        detail = {}
        try:
            detail = resp.json().get("detail", {})
        except Exception:
            pass
        # This route can also 403 with {"error": "tenant_denied"} -- the caller
        # authenticated fine but doesn't own cfg['project_id'], distinct remediation from
        # a missing scope, so it gets its own error code/exit status (22).
        if detail.get("error") == "tenant_denied":
            fail(None, "tenant_denied",
                 f"This registry's tenant boundary rejected the request — your "
                 f"credential does not own project '{cfg['project_id']}'.")
        fail(None, "scope_denied",
             f"Token lacks the 'admin' scope required to "
             f"{'freeze' if frozen else 'unfreeze'} this project "
             f"({detail.get('required_scope', 'admin')} required).")
    if resp.status_code != 200:
        fail(None, "validation", f"Registry rejected the freeze request: {resp.text[:200]}")
    return resp.json()


@freeze.command("on")
@click.option("--reason", default=None, help="Why the project is being frozen.")
def freeze_on(reason: str | None) -> None:
    """Freeze the project: only reads and rollback remain permitted."""
    repo_root = ensure_repo()
    body = _set_freeze(repo_root, True, reason)
    if current_output_mode() == "json":
        emit_json(None, "freeze on", data=body)
        return
    click.secho(f"Project frozen{f' ({reason})' if reason else ''}.", fg="red", bold=True)


@freeze.command("off")
def freeze_off() -> None:
    """Unfreeze the project."""
    repo_root = ensure_repo()
    body = _set_freeze(repo_root, False, None)
    if current_output_mode() == "json":
        emit_json(None, "freeze off", data=body)
        return
    click.secho("Project unfrozen.", fg="green")


@click.command("incident")
@click.argument("action", type=click.Choice(["rollback"]))
@click.option("--reason", default="incident rollback", show_default=True)
def incident(action: str, reason: str) -> None:
    """`av incident rollback` — freeze, then roll back the active improver in one command."""
    repo_root = ensure_repo()
    _set_freeze(repo_root, True, reason)
    from .cmd_improver import improver_rollback

    ctx = click.get_current_context()
    if current_output_mode() != "json":
        ctx.invoke(improver_rollback, target_id=None)
        return

    # improver_rollback() emits its own top-level JSON envelope -- invoking it unguarded
    # would print a second JSON object for one `av incident rollback` call, so its stdout
    # is captured and folded in under `rollback` instead.
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ctx.invoke(improver_rollback, target_id=None)
    line = buf.getvalue().strip().splitlines()[-1] if buf.getvalue().strip() else "{}"
    rollback_data = json.loads(line).get("data")
    emit_json(None, "incident rollback", data={"frozen": True, "rollback": rollback_data})
