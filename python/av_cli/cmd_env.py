"""av env — environment snapshot & replay recipes (v1.2.0; CAS-backed in v1.2.2).

Recipe-exact, not bit-exact: captures the interpreter + curated package pins + seeds
into .av/env_snapshot.json (embedded into .avh replay), and renders reproduction
instructions. Full pip freeze via --full for maximal fidelity.

v1.2.2 env snapshot/replay: every snapshot is content-addressed — its canonical
(sorted-keys, timestamp-stripped) JSON is hashed, and that hash IS the id. The snapshot
object uploads through the NORMAL object flow at push time (`core.upload_commit_objects`
picks it up), commits carry `env_snapshot_id`, runs back-fill it server-side on first
link, and `av replay <run|commit>` loads it from the local CAS or the registry.
"""
import datetime
import json
import os
import pathlib
import sys

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import (
    current_output_mode,
    emit_json,
    env_snapshot_file,
    env_snapshot_id,
    fail,
)

# Curated pins: the packages that dominate training reproducibility in practice.
_CURATED = ["torch", "torchvision", "lightning", "pytorch_lightning", "transformers",
            "datasets", "numpy", "pandas", "safetensors", "scikit-learn", "mlflow",
            "aether-vault"]


def _collect(full: bool) -> dict:
    from importlib.metadata import version

    def v(name: str) -> str | None:
        try:
            return version(name)
        except Exception:
            return None

    seeds: dict = {}
    for var in ("SEED", "RANDOM_SEED", "PL_GLOBAL_SEED", "PYTHONHASHSEED"):
        if os.environ.get(var):
            seeds[var] = os.environ[var]
    try:  # best-effort torch seed introspection (never fatal)
        import torch  # type: ignore

        if hasattr(torch, "initial_seed"):
            seeds["torch_initial_seed"] = str(torch.initial_seed())
    except Exception:
        pass

    snap: dict = {
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "seeds": seeds,
        "pins": {name: ver for name in _CURATED if (ver := v(name))},
    }
    if full:
        try:
            from importlib.metadata import distributions

            snap["freeze"] = sorted(f"{d.metadata['Name']}=={d.version}"
                                    for d in distributions() if d.metadata.get("Name"))
        except Exception:
            pass
    return snap


def _store_snapshot_object(repo_root: Path, snap: dict) -> str:
    """Writes the snapshot into the local CAS under its canonical hash. Returns the id.

    The CAS object holds the CANONICAL bytes (compact, timestamp-stripped) — exactly the
    bytes the id hashes, so the registry's own sha256 verification accepts the upload and
    any clone can re-derive the same id from the downloaded object. The human-readable
    pretty file stays at .av/env_snapshot.json; registry upload happens through the
    normal push flow (upload_commit_objects), never a side channel."""
    sid = env_snapshot_id(snap)
    obj_path = repo_root / ".av" / "objects" / sid[:2] / sid[2:]
    if not obj_path.exists():
        obj_path.parent.mkdir(parents=True, exist_ok=True)
        obj_path.write_bytes(canonical_env_bytes(snap))
    return sid


def _link_run_state(repo_root: Path, sid: str) -> bool:
    """Records the snapshot id on an ACTIVE run's local state so `av run start`-style
    payloads carry it (the server back-fills run.env_snapshot_id on first linked commit)."""
    state_path = repo_root / ".av" / "run.json"
    if not state_path.exists():
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(state, dict):
        return False
    existing = state.get("env_snapshot_id")
    if existing == sid:
        return True
    state["env_snapshot_id"] = sid
    atomic_write_text(state_path, json.dumps(state))
    return True


def fetch_snapshot_by_id(repo_root: Path, client, sid: str) -> dict | None:
    """Loads a snapshot object by id — local CAS first, then the registry.

    Downloaded snapshots land in the local CAS like any other object so repeat replays
    work offline afterward."""
    obj_path = repo_root / ".av" / "objects" / sid[:2] / sid[2:]
    if obj_path.exists():
        try:
            snap = json.loads(obj_path.read_text(encoding="utf-8"))
            if isinstance(snap, dict):
                return snap
        except (OSError, ValueError):
            pass
    if client is None or not client.server_available():
        return None
    try:
        resp = client.session.get(f"{client.server_url}/api/objects/{sid}", timeout=60)
        if resp.status_code != 200:
            return None
        snap = json.loads(resp.content.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(snap, dict) or env_snapshot_id(snap) != sid:
        return None  # corrupt or mis-labeled object: refuse rather than guess
    obj_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(obj_path, json.dumps(snap, indent=2, sort_keys=True))
    return snap


def resolve_replay_target(repo_root: Path, target: str | None):
    """Resolves `target` to (snapshot_dict, source_description).

    Target forms:
      - None           → this repo's .av/env_snapshot.json (as always)
      - <run-id>       → GET /api/runs/<id> → its env_snapshot_id → fetch object
      - <commit-hash>  → local commit file or registry → payload's env_snapshot_id
      - <snapshot-id>  → direct object fetch by canonical hash
    """
    client = None
    try:
        from .client import VaultClient

        cfg = load_config(repo_root)
        client = VaultClient(cfg.get("remote_url", "http://localhost:8000"),
                             cfg.get("remote_api_token"))
    except Exception:
        client = None

    def _by_sid(sid: str) -> dict | None:
        return fetch_snapshot_by_id(repo_root, client, sid)

    if target is None:
        loaded = load_env_snapshot(repo_root)
        return (loaded[1], "local") if loaded else (None, None)

    # Run id (UUID-ish, contains dashes) or commit/snapshot hash (64 hex chars).
    if "-" in target:
        if client is None or not client.server_available():
            return None, None
        try:
            resp = client.session.get(f"{client.server_url}/api/runs/{target}", timeout=30)
            sid = resp.json().get("env_snapshot_id") if resp.status_code == 200 else None
        except Exception:
            return None, None
        if not sid:
            return None, None
        return _by_sid(sid), f"run {target}"

    # Commit hash: prefer local history, fall back to the registry.
    from .handoff import load_commit

    commit = load_commit(repo_root, target)
    sid = (commit or {}).get("env_snapshot_id")
    if sid:
        snap = _by_sid(sid)
        if snap is not None:
            return snap, f"commit {target[:12]}"

    # Snapshot id itself?
    snap = _by_sid(target)
    if snap is not None:
        return snap, "snapshot id"
    return None, None


def render_recipe(snap: dict, dockerfile: bool) -> str:
    """Pure renderer shared by all replay paths (and the golden fixture test)."""
    pins = snap.get("freeze") or [f"{k}=={v}" for k, v in (snap.get("pins") or {}).items()]
    install_block = "\n".join(f"RUN pip install {pin}" for pin in pins) if dockerfile else \
        "\n".join(f"  pip install {pin}" for pin in pins)
    if dockerfile:
        recipe = (
            "# Recipe-exact base — adjust CUDA tag to match your drivers.\n"
            "FROM python:" + snap.get("python", "3.12") + "-slim\n\n"
            + install_block + "\n\n"
            "# Data & weights come from Aether itself:\n"
            "#   av clone <project> --remote-url <registry>\n"
            "#   av checkout <commit>\n"
            f"# Seeds / CUDA_VISIBLE_DEVICES at capture time:\n"
            f"#   CUDA_VISIBLE_DEVICES={snap.get('cuda_visible_devices')}\n"
        )
    else:
        recipe = (
            f"python=={snap.get('python')}\n"
            f"CUDA_VISIBLE_DEVICES={snap.get('cuda_visible_devices')}\n"
            "packages:\n" + install_block
        )
    return recipe


@click.group()
def env() -> None:
    """Environment snapshots and replay recipes for runs/commits."""


@env.command()
@click.option("--full", is_flag=True, default=False, help="Include a full pip freeze.")
def snapshot(full: bool) -> None:
    """Capture the current environment into the repo (.av/env_snapshot.json + CAS)."""
    repo_root = ensure_repo()
    snap = _collect(full)
    out = env_snapshot_file(repo_root)
    atomic_write_text(out, json.dumps(snap, indent=2, sort_keys=True))
    sid = _store_snapshot_object(repo_root, snap)
    linked = _link_run_state(repo_root, sid)
    if current_output_mode() == "json":
        emit_json(None, "env snapshot", data={"path": str(out), "id": sid,
                                              "pins": snap["pins"],
                                              "full": bool(snap.get("freeze")),
                                              "linked_to_active_run": linked})
        return
    click.secho(f"Snapshot written ({len(snap['pins'])} curated pins"
                f"{', full freeze' if snap.get('freeze') else ''}).", fg="green")
    click.secho(f"  id: {sid[:16]}…  (uploaded with your next push)"
                + ("  · linked to the active run" if linked else ""), fg="cyan")


@env.command()
@click.argument("target", required=False, default=None)
@click.option("--dockerfile", is_flag=True, default=False, help="Emit a Dockerfile draft.")
@click.option("--execute", "execute_mode", is_flag=True, default=False,
              help="Execute the recipe's pip installs after showing it (asks first unless -y).")
@click.option("-y", "--yes", is_flag=True, default=False, help="Skip the execute confirmation.")
def replay(target: str | None, dockerfile: bool, execute_mode: bool, yes: bool) -> None:
    """Print (or execute) the reproduction recipe for TARGET (a run id, commit hash, or
    snapshot id) — or the latest local snapshot when omitted.

    Also reachable as the top-level `av replay <target>` alias (v1.2.2). TARGET
    resolution loads the snapshot from the local CAS or the registry (snapshots ride the
    normal object flow at push), so any clone can reproduce an experiment's environment
    without touching the authoring machine. On another machine, resolve by RUN id (runs
    carry env_snapshot_id server-side) or by the raw snapshot id from `.avh.replay` —
    commit-payload ids live in local commit files only.
    """
    repo_root = ensure_repo()
    snap, source = resolve_replay_target(repo_root, target)
    if snap is None:
        fail(None, "validation",
             "No snapshot found — `av env snapshot` first, or pass a run/commit whose "
             "environment was captured and pushed.")

    recipe = render_recipe(snap, dockerfile)
    if source and source != "local":
        click.secho(f"# snapshot {source}", fg="cyan")

    click.echo(recipe)

    if execute_mode:
        pip_lines = [
            ln.strip()[len("pip install "):]
            for ln in recipe.splitlines() if ln.strip().startswith("pip install ")
        ]
        if not pip_lines:
            fail(None, "validation", "Nothing to install — snapshot has no pins.")
        import subprocess as _sp

        if not yes:
            click.secho(f"About to run {len(pip_lines)} pip install command(s) "
                        "in THIS interpreter. Continue? [y/N]", fg="yellow", nl=False)
            if input().strip().lower() not in ("y", "yes"):
                click.secho("Aborted — nothing executed.", fg="yellow")
                return
        for pin in pip_lines:
            rc = _sp.call(["pip", "install", pin])
            if rc != 0:
                fail(None, "validation", f"pip install failed for: {pin}")
        click.secho("Environment reproduced (pip level).", fg="green")

    if current_output_mode() == "json":
        emit_json(None, "env replay", data={"recipe": recipe, "snapshot": snap,
                                            "source": source or "local",
                                            "executed": bool(execute_mode)})
        return
