"""av run — first-class Experiment/Run lifecycle (v1.2.0).

A run groups the commits of one training effort. `av run start` registers server-side
when reachable and ALWAYS writes local state (.av/run.json) so offline agents keep
grouping; the server lazily creates unknown runs at push time, so ordering never
matters. AV_RUN_ID overrides/feeds the same state (agents set it per process).
"""

import json
import os
import uuid

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import EXIT_UNREACHABLE_QUEUED, current_output_mode, emit_json


def _state_path(repo_root):
    return repo_root / ".av" / "run.json"


def _client(repo_root):
    from .client import VaultClient

    cfg = load_config(repo_root)
    return VaultClient(cfg.get("remote_url", "http://localhost:8000"),
                       cfg.get("remote_api_token"))


def _register_remote(repo_root, payload: dict) -> tuple[bool, dict | None]:
    """POST /api/runs; returns (registered, response_dict_or_None). Never raises."""
    client = _client(repo_root)
    if not client.server_available():
        return False, None
    try:
        resp = client.session.post(f"{client.server_url}/api/runs", json=payload)
        if resp.status_code == 200:
            return True, resp.json()
    except Exception:
        pass
    return False, None


@click.group()
def run() -> None:
    """Experiment runs: group commits, track lineage and metrics summaries."""


@run.command()
@click.argument("name", required=False)
@click.option("--parent", "parent_run_id", default=None,
              help="Parent run id (fine-tune descended from that run).")
def start(name: str | None, parent_run_id: str | None) -> None:
    """Start a run; every subsequent commit is filed under it (also via AV_RUN_ID)."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    run_id = str(uuid.uuid4())
    code_pointer = None
    try:
        import subprocess

        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root,
                             capture_output=True, text=True, timeout=10)
        sha = out.stdout.strip() or None
        if sha:
            remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_root,
                                    capture_output=True, text=True, timeout=10
                                    ).stdout.strip() or None
            dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=repo_root,
                                        capture_output=True, text=True,
                                        timeout=10).stdout.strip())
            code_pointer = {"git_remote": remote, "git_sha": sha, "dirty": dirty}
    except (OSError, subprocess.TimeoutExpired):
        pass

    # v1.2.2 env snapshot/replay: an already-captured snapshot links to the run at
    # registration AND in local state, so the registry can answer
    # "what environment did this run use?" from day one.
    loaded_snapshot = load_env_snapshot(repo_root)
    env_snapshot_id = loaded_snapshot[0] if loaded_snapshot else None

    registered, _ = _register_remote(repo_root, {
        # v1.2.5 fix: project_id was missing here entirely — the server's POST /api/runs
        # requires it (422 without one), so EVERY `av run start` registration has been
        # silently failing (_register_remote swallows any non-200 status) and falling
        # back to the server's lazy-create-at-push path, which has no way to learn the
        # run's `name` (the commit payload never carries it) -- runs registered this way
        # were created, but permanently nameless. See Probleme.md.
        "id": run_id, "project_id": cfg["project_id"], "name": name,
        "parent_run_id": parent_run_id, "code_pointer": code_pointer,
        **({"env_snapshot_id": env_snapshot_id} if env_snapshot_id else {}),
    })

    state = {"run_id": run_id, "name": name, "status": "running",
             "parent_run_ids": [parent_run_id] if parent_run_id else [],
             "code_pointer": code_pointer, "started_at": True,
             **({"env_snapshot_id": env_snapshot_id} if env_snapshot_id else {})}
    _state_path(repo_root).write_text(json.dumps(state), encoding="utf-8")

    if current_output_mode() == "json":
        emit_json(None, "run start", data={"run_id": run_id, "name": name,
                                           "registered_server_side": registered})
        return
    where = "server + local" if registered else "LOCAL ONLY (will register on first push)"
    click.secho(f"Run {run_id} started ({where}). Commits now carry run:{run_id}.", fg="green")


@run.command()
@click.option("--fail", "as_failed", is_flag=True, default=False, help="Mark as failed.")
@click.option("--metric", "metrics_raw", multiple=True, help="key=value final metric.")
def finish(metrics_raw: tuple, as_failed: bool) -> None:
    """Complete (or fail) the active run and clear local state."""
    repo_root = ensure_repo()
    path = _state_path(repo_root)
    if not path.exists():
        fail(None, "validation", "No active run — `av run start` first.")
    state = json.loads(path.read_text(encoding="utf-8"))
    run_id = state.get("run_id")

    summary: dict = {}
    for raw in metrics_raw:
        if "=" in raw:
            k, v = raw.split("=", 1)
            try:
                summary[k.strip()] = float(v) if "." in v else int(v)
            except ValueError:
                summary[k.strip()] = v

    status = "failed" if as_failed else "completed"
    endpoint = f"/api/runs/{run_id}/{'fail' if as_failed else 'complete'}"
    client = _client(repo_root)
    delivered = False
    if client.server_available():
        try:
            resp = client.session.post(f"{client.server_url}{endpoint}",
                                       json={"metrics_summary": summary})
            delivered = resp.status_code == 200
        except Exception:
            delivered = False

    path.unlink(missing_ok=True)
    if current_output_mode() == "json":
        emit_json(None, "run finish", data={"run_id": run_id, "status": status,
                                            "metrics_summary": summary,
                                            "delivered_to_registry": delivered})
        return
    click.secho(f"Run {run_id} → {status}{' (registry updated)' if delivered else ' (local only)'}",
                fg="green" if not as_failed else "yellow")


@run.command("list")
@click.option("--project", "project_id", default=None, help="Defaults to this repo's project.")
@click.option("--status", "status_filter", default=None)
@click.option("--limit", default=20, show_default=True)
def list_runs(project_id: str | None, status_filter: str | None, limit: int) -> None:
    """List runs known to the registry."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    pid = project_id or cfg.get("project_id")
    client = _client(repo_root)
    params = {"project_id": pid, "limit": limit}
    if status_filter:
        params["status"] = status_filter
    try:
        resp = client.session.get(f"{client.server_url}/api/runs", params=params)
        rows = resp.json().get("runs", []) if resp.status_code == 200 else []
    except Exception:
        rows = []
    if current_output_mode() == "json":
        emit_json(None, "run list", data={"runs": rows})
        return
    if not rows:
        click.secho("No runs on the registry yet.", fg="yellow")
        return
    for r in rows:
        ms = r.get("metrics_summary") or {}
        tail = ", ".join(f"{k}={v}" for k, v in list(ms.items())[:3])
        click.echo(f"  [{r['status']:>9}] {r['id'][:8]}  {(r.get('name') or '-'):<20} {tail}")


@run.command()
@click.argument("run_id")
def show(run_id: str) -> None:
    """Show one run incl. linked commits."""
    client = _client(ensure_repo())
    try:
        resp = client.session.get(f"{client.server_url}/api/runs/{run_id}")
        body = resp.json()
    except Exception:
        fail(None, "validation", "Registry unreachable or run unknown.")
    if current_output_mode() == "json":
        emit_json(None, "run show", data=body)
        return
    click.secho(f"Run {body['id']} [{body['status']}] {body.get('name') or ''}", bold=True)
    cp = body.get("code_pointer") or {}
    if cp.get("git_sha"):
        dirty = "dirty" if cp.get("dirty") else "clean"
        click.echo(f"  code: {cp.get('git_remote')}@{str(cp['git_sha'])[:10]} ({dirty})")
    for k, v in (body.get("metrics_summary") or {}).items():
        click.echo(f"  {k}: {v}")
    hashes = body.get("commit_hashes") or []
    click.echo(f"  commits: {len(hashes)}")


def current_run_id(repo_root) -> str | None:
    """The active run id, if any (used by commit's auto-tagging).

    v1.2.5: delegates to core.resolve_run_id() (explicit > AV_RUN_ID env > .av/run.json
    state) — kept as a thin wrapper so `av_sdk.Repo` and any other existing import of
    this name keep working unchanged; the actual precedence logic lives in one place now.
    """
    from .core import resolve_run_id

    return resolve_run_id(repo_root)
