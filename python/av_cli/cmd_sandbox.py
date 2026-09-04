"""av sandbox — pluggable sandbox executor (v1.3.1, RSI R5: todo.md G.29, G.32).

`av sandbox run` resolves a driver (`python/av_cli/sandbox/`), checks the command against
the given improver version's tool permission manifest FIRST (`--improver`, defaults to
maximally restrictive if omitted — see `sandbox/manifest.py`), then submits. Reporting the
job to the registry (`POST /api/sandbox/jobs`) is best-effort telemetry — same contract as
`_report_policy_outcome` — a reachability failure never blocks the actual sandboxed
execution, matching AGENTS.md non-negotiable #3 even though sandbox jobs are a new
surface: `local` jobs in particular have real value fully offline.
"""
import json

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, resolve_remote

_DRIVER_NAMES = ("local", "docker", "kubernetes", "slurm")


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


def _parse_mount(raw: str):
    from .sandbox.base import Mount

    parts = raw.split(":")
    if len(parts) not in (2, 3):
        fail(None, "validation", f'Malformed --mount {raw!r} — expected "host:container[:ro|rw]".')
    host, container = parts[0], parts[1]
    mode = parts[2] if len(parts) == 3 else "ro"
    if mode not in ("ro", "rw"):
        fail(None, "validation", f"Mount mode must be 'ro' or 'rw', got {mode!r}.")
    return Mount(host=host, container=container, mode=mode)


def _report_job(repo_root, job_id: str, driver: str, improver_id: str | None,
                command: list[str], state: str) -> None:
    """Best-effort telemetry — never raises, never blocks the sandbox operation itself."""
    try:
        client = _client(repo_root)
        if not client.server_available():
            return
        cfg = load_config(repo_root)
        client.session.post(f"{client.server_url}/api/sandbox/jobs", json={
            "id": job_id, "project_id": cfg["project_id"], "improver_id": improver_id,
            "driver": driver, "command": command, "state": state,
        })
    except Exception:
        pass


def _report_status(repo_root, job_id: str, state: str, exit_code: int | None) -> None:
    try:
        client = _client(repo_root)
        if not client.server_available():
            return
        client.session.post(f"{client.server_url}/api/sandbox/jobs/{job_id}/status",
                            json={"state": state, "exit_code": exit_code})
    except Exception:
        pass


@click.group()
def sandbox() -> None:
    """Pluggable sandbox executor: local/docker/kubernetes/slurm, one protocol."""


@sandbox.command("run")
@click.argument("command", nargs=-1, required=True)
@click.option("--driver", type=click.Choice(_DRIVER_NAMES), default="local", show_default=True)
@click.option("--job-id", "job_id", default=None, help="Defaults to a fresh uuid.")
@click.option("--improver", "improver_id", default=None,
              help="Enforce this improver version's tool manifest (default: maximally restrictive).")
@click.option("--mount", "mounts_raw", multiple=True, help='"host:container[:ro|rw]" (repeatable).')
@click.option("--network", type=click.Choice(["none", "bridge"]), default="none", show_default=True)
@click.option("--cpu", "cpu_limit", type=float, default=None)
@click.option("--memory-mb", "memory_limit_mb", type=int, default=None)
@click.option("--gpu", is_flag=True, default=False)
@click.option("--timeout", "timeout_secs", type=int, default=3600, show_default=True)
def sandbox_run(command: tuple, driver: str, job_id: str | None, improver_id: str | None,
                mounts_raw: tuple, network: str, cpu_limit: float | None,
                memory_limit_mb: int | None, gpu: bool, timeout_secs: int) -> None:
    """Run COMMAND inside DRIVER's sandbox. A violation of the improver's tool manifest
    aborts BEFORE anything runs — never a runtime surprise."""
    import uuid as _uuid

    from .sandbox.base import JobSpec, get_driver
    from .sandbox.manifest import load_manifest, verify_spec_against_manifest

    repo_root = ensure_repo()
    job_id = job_id or str(_uuid.uuid4())
    spec = JobSpec(job_id=job_id, command=list(command), cwd=repo_root,
                  mounts=[_parse_mount(m) for m in mounts_raw], network=network,
                  cpu_limit=cpu_limit, memory_limit_mb=memory_limit_mb, gpu=gpu,
                  timeout_secs=timeout_secs)

    manifest = load_manifest(repo_root, improver_id or "")
    ok, reason = verify_spec_against_manifest(spec, manifest)
    if not ok:
        _report_job(repo_root, job_id, driver, improver_id, list(command), "failed")
        fail(None, "validation", f"Tool manifest violation: {reason}")

    drv = get_driver(driver, repo_root)
    status = drv.submit(spec)
    _report_job(repo_root, job_id, driver, improver_id, list(command), status.state)
    if current_output_mode() == "json":
        emit_json(None, "sandbox run", data={"job_id": job_id, "driver": driver,
                                             "state": status.state, "exit_code": status.exit_code,
                                             "message": status.message})
        if status.state == "failed":
            raise SystemExit(EXIT_VALIDATION)
        return
    click.secho(f"[{status.state}] job {job_id} ({driver})"
               + (f" exit={status.exit_code}" if status.exit_code is not None else ""),
               fg="green" if status.state in ("succeeded", "running") else "red")
    if status.state == "failed":
        raise SystemExit(EXIT_VALIDATION)


@sandbox.command("status")
@click.argument("job_id")
@click.option("--driver", type=click.Choice(_DRIVER_NAMES), required=True)
def sandbox_status(job_id: str, driver: str) -> None:
    from .sandbox.base import get_driver

    repo_root = ensure_repo()
    status = get_driver(driver, repo_root).status(job_id)
    _report_status(repo_root, job_id, status.state, status.exit_code)
    if current_output_mode() == "json":
        emit_json(None, "sandbox status", data={"job_id": job_id, "state": status.state,
                                                 "exit_code": status.exit_code, "message": status.message})
        return
    click.echo(f"{job_id}: {status.state}" + (f" (exit {status.exit_code})" if status.exit_code is not None else ""))


@sandbox.command("cancel")
@click.argument("job_id")
@click.option("--driver", type=click.Choice(_DRIVER_NAMES), required=True)
def sandbox_cancel(job_id: str, driver: str) -> None:
    from .sandbox.base import get_driver

    repo_root = ensure_repo()
    cancelled = get_driver(driver, repo_root).cancel(job_id)
    if cancelled:
        _report_status(repo_root, job_id, "cancelled", None)
    if current_output_mode() == "json":
        emit_json(None, "sandbox cancel", data={"job_id": job_id, "cancelled": cancelled})
        return
    click.secho(f"Job {job_id}: {'cancelled' if cancelled else 'nothing to cancel (not running)'}",
               fg="yellow" if cancelled else "green")


@sandbox.command("logs")
@click.argument("job_id")
@click.option("--driver", type=click.Choice(_DRIVER_NAMES), required=True)
def sandbox_logs(job_id: str, driver: str) -> None:
    from .sandbox.base import get_driver

    repo_root = ensure_repo()
    output = get_driver(driver, repo_root).logs(job_id)
    if current_output_mode() == "json":
        emit_json(None, "sandbox logs", data={"job_id": job_id, "output": output})
        return
    click.echo(output)


@click.command("replay-actions")
@click.argument("target")
@click.option("--execute", is_flag=True, default=False,
              help="Actually re-run each logged action's command via the local driver "
                   "(default: print the recorded decision sequence only, no execution).")
def replay_actions(target: str, execute: bool) -> None:
    """Replay agent DECISIONS (todo.md G.31), not just training code — TARGET is an
    action-log id or a run id. Combine with `av env replay <snapshot-id> --execute`
    first for a full environment-plus-decisions reproduction; this command deliberately
    does not auto-chain that heavier step itself."""
    from . import casobj
    from .actionlog import read_actions
    from .sandbox.base import JobSpec, get_driver

    repo_root = ensure_repo()
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued", "Registry unreachable — cannot fetch the action log.")

    resp = client.session.get(f"{client.server_url}/api/action-logs/{target}")
    if resp.status_code != 200:
        resp = client.session.get(f"{client.server_url}/api/action-logs", params={"run_id": target})
        rows = resp.json().get("action_logs", []) if resp.status_code == 200 else []
        if not rows:
            fail(None, "validation", f"Unknown action log or run: {target}")
        row = rows[0]
    else:
        row = resp.json()

    doc = casobj.read_object(repo_root, row["object_id"])
    if doc is None and client.download_object(row["object_id"], casobj.object_path(repo_root, row["object_id"])):
        doc = casobj.read_object(repo_root, row["object_id"])
    actions = (doc or {}).get("actions", [])

    results = []
    if execute:
        driver = get_driver("local", repo_root)
        for i, action in enumerate(actions):
            command = action.get("command")
            if not command:
                continue
            job_id = f"replay-{row['id'][:8]}-{i}"
            status = driver.submit(JobSpec(job_id=job_id, command=command, cwd=repo_root))
            results.append({"index": i, "action": action["action"], "state": status.state,
                            "exit_code": status.exit_code})

    if current_output_mode() == "json":
        emit_json(None, "replay-actions", data={"log_id": row["id"], "actions": actions,
                                                 "executed": results})
        return
    click.secho(f"Action log {row['id']} ({len(actions)} action(s)):", bold=True)
    for a in actions:
        click.echo(f"  [{a.get('ts', '')}] {a.get('actor', '-')}: {a['action']}")
    if results:
        click.secho("\nReplay results:", bold=True)
        for r in results:
            color = "green" if r["state"] == "succeeded" else "red"
            click.secho(f"  [{r['state']}] #{r['index']} {r['action']} (exit {r['exit_code']})",
                       fg=color)


@sandbox.command("queue")
@click.option("--project", "project_id", default=None)
@click.option("--state", "state_filter", type=click.Choice(
    ["pending", "running", "succeeded", "failed", "cancelled"]), default=None)
def sandbox_queue(project_id: str | None, state_filter: str | None) -> None:
    """List sandbox jobs recorded on the registry — across drivers/machines (todo.md
    G.32's "queue" surface); a `local` job on someone else's laptop has no listing
    capability of its own, this is what makes it visible anyway."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued", "Registry unreachable — the sandbox queue is server-authoritative.")
    params = {"project_id": project_id or cfg.get("project_id")}
    if state_filter:
        params["state"] = state_filter
    resp = client.session.get(f"{client.server_url}/api/sandbox/jobs", params=params)
    rows = resp.json().get("jobs", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "sandbox queue", data={"jobs": rows})
        return
    for r in rows:
        click.echo(f"  [{r['state']:>9}] {r['id'][:8]} ({r['driver']})")
