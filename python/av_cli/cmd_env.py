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
import platform as _platform
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

# v1.2.5: env vars captured into the HASHED `env.env_vars` (they change training
# behavior, so they're part of identity) — overridable via AV_ENV_CAPTURE_VARS
# (comma-separated) for projects with their own critical vars.
_DEFAULT_CAPTURE_VARS = [
    "CUDA_VISIBLE_DEVICES", "PYTORCH_CUDA_ALLOC_CONF", "OMP_NUM_THREADS",
    "TOKENIZERS_PARALLELISM", "HF_HOME", "TORCH_HOME",
]


def _capture_var_names() -> list[str]:
    raw = os.environ.get("AV_ENV_CAPTURE_VARS")
    if raw:
        return [v.strip() for v in raw.split(",") if v.strip()]
    return list(_DEFAULT_CAPTURE_VARS)


def _gpu_and_cuda_info() -> dict:
    """Best-effort GPU/driver/CUDA-toolkit introspection — every probe is wrapped so a
    missing nvidia-smi/nvcc/torch (the overwhelmingly common non-GPU-machine case) never
    fails the capture; it just leaves these fields None/empty."""
    import re
    import subprocess

    names: list[str] = []
    driver_version = None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            for line in out.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if parts and parts[0]:
                    names.append(parts[0])
                    if len(parts) > 1 and parts[1]:
                        driver_version = parts[1]
    except Exception:
        pass

    if not names:
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                names = [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]
        except Exception:
            pass

    cuda_toolkit_version = None
    try:
        out = subprocess.run(["nvcc", "--version"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            m = re.search(r"release (\d+\.\d+)", out.stdout)
            if m:
                cuda_toolkit_version = m.group(1)
    except Exception:
        pass
    if cuda_toolkit_version is None:
        try:
            import torch  # type: ignore

            cuda_toolkit_version = getattr(torch.version, "cuda", None)
        except Exception:
            pass

    return {
        "gpu_names": names,
        "driver_version": driver_version,
        "device_count": len(names),
        "cuda_toolkit_version": cuda_toolkit_version,
    }


def _safe_hostname() -> str | None:
    try:
        import socket

        return socket.gethostname()
    except Exception:
        return None


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

    pins = {name: ver for name in _CURATED if (ver := v(name))}
    gpu = _gpu_and_cuda_info()
    env_vars = {name: os.environ[name] for name in _capture_var_names() if name in os.environ}

    # snapshot_version 2 (v1.2.5): split into HASHED identity (`env`, reproducibility-
    # relevant) vs unhashed OBSERVED context (`observed`, machine-specific) — see
    # core.py::canonical_env_bytes for the rationale. Top-level flat fields (python,
    # platform, cuda_visible_devices, seeds, pins) are kept for backward compatibility
    # with every existing reader (render_recipe, .avh replay section, etc.) — `env`/
    # `observed` exist purely to define what participates in the snapshot's identity.
    snap: dict = {
        "snapshot_version": 2,
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "seeds": seeds,
        "pins": pins,
        "env": {
            "python": sys.version.split()[0],
            "os_family": _platform.system(),
            "pins": pins,
            "seeds": seeds,
            "cuda_toolkit_version": gpu["cuda_toolkit_version"],
            "env_vars": env_vars,
        },
        "observed": {
            "gpu_names": gpu["gpu_names"],
            "driver_version": gpu["driver_version"],
            "device_count": gpu["device_count"],
            "hostname": _safe_hostname(),
            "conda_env": os.environ.get("CONDA_DEFAULT_ENV"),
            "interpreter": {
                "executable": sys.executable,
                "prefix": sys.prefix,
                "base_prefix": getattr(sys, "base_prefix", sys.prefix),
                "conda_prefix": os.environ.get("CONDA_PREFIX"),
            },
        },
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


def render_recipe(snap: dict, dockerfile: bool, cuda_tag: str | None = None) -> str:
    """Pure renderer shared by all replay paths (and the golden fixture test).

    v1.2.5: `cuda_tag` switches the Dockerfile base to an nvidia/cuda runtime image
    (e.g. "12.1.0") instead of plain python:slim, and the Dockerfile itself became
    multi-stage (builder installs pins into a venv, runtime copies just that venv) with
    a non-root user — closer to what a real training image looks like."""
    pins = snap.get("freeze") or [f"{k}=={v}" for k, v in (snap.get("pins") or {}).items()]
    py_version = snap.get("python", "3.12")
    if dockerfile:
        if cuda_tag:
            builder_base = f"nvidia/cuda:{cuda_tag}-runtime-ubuntu22.04"
            runtime_base = builder_base
            py_install = (
                f"RUN apt-get update && apt-get install -y --no-install-recommends "
                f"python{py_version.rsplit('.', 1)[0]} python3-pip python3-venv && \\\n"
                "    rm -rf /var/lib/apt/lists/*\n"
            )
        else:
            builder_base = f"python:{py_version}-slim"
            runtime_base = builder_base
            py_install = ""
        pip_lines = "\n".join(f"RUN /opt/venv/bin/pip install --no-cache-dir {pin}" for pin in pins)
        recipe = (
            "# syntax=docker/dockerfile:1\n"
            "# Recipe-exact reproduction of a captured training environment (av env replay "
            "--dockerfile).\n"
            f"# Adjust the CUDA tag ({cuda_tag or '(none — CPU base)'}) to match your drivers "
            "if this drifts.\n\n"
            f"FROM {builder_base} AS builder\n"
            + py_install +
            "RUN python3 -m venv /opt/venv\n"
            f"{pip_lines}\n\n"
            f"FROM {runtime_base}\n"
            + py_install +
            "RUN useradd --create-home --shell /bin/bash av\n"
            "COPY --from=builder /opt/venv /opt/venv\n"
            "ENV PATH=\"/opt/venv/bin:$PATH\"\n"
            "USER av\n"
            "WORKDIR /workspace\n\n"
            "# Data & weights come from Aether itself:\n"
            "#   av clone <project> --remote-url <registry>\n"
            "#   av checkout <commit>\n"
            f"# Seeds / CUDA_VISIBLE_DEVICES at capture time:\n"
            f"#   CUDA_VISIBLE_DEVICES={snap.get('cuda_visible_devices')}\n"
        )
    else:
        install_block = "\n".join(f"  pip install {pin}" for pin in pins)
        recipe = (
            f"python=={py_version}\n"
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


def _resolve_pip_invocation(target_venv: str | None, conda_env: str | None):
    """v1.2.5: where --execute actually installs. Returns (argv_prefix, description).

    Default (neither flag) now correctly uses `sys.executable -m pip` — the pre-1.2.5
    code shelled a bare `pip`, which can silently resolve to a DIFFERENT interpreter's
    pip than the one running this command (Probleme.md: a real interpreter-mismatch
    risk, not hypothetical, on any machine with more than one Python on PATH)."""
    import shutil

    if conda_env:
        if shutil.which("conda") is None:
            fail(None, "validation",
                 "--conda-env was given but `conda` is not on PATH — install/activate "
                 "conda first, or use --target-venv instead.")
        return ["conda", "run", "-n", conda_env, "python", "-m", "pip"], f"conda env '{conda_env}'"
    if target_venv:
        venv_path = pathlib.Path(target_venv)
        if not venv_path.exists():
            import venv as _venv_mod

            click.secho(f"Creating venv at {venv_path}...", fg="cyan")
            _venv_mod.create(venv_path, with_pip=True)
        py = venv_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        activate = (venv_path / "Scripts" / "activate") if os.name == "nt" else \
            (venv_path / "bin" / "activate")
        return [str(py), "-m", "pip"], f"venv at {venv_path} (activate: {activate})"
    return [sys.executable, "-m", "pip"], f"the running interpreter ({sys.executable})"


def _validate_pins(pins: list[str]) -> list[dict]:
    """v1.2.5: resolves each pin via `pip install --dry-run` (real dependency
    resolution against PyPI, no package actually written to disk) — this is
    '--validate', the answer to "can this recipe resolve?" without installing anything."""
    import subprocess

    results = []
    for pin in pins:
        try:
            out = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
                 "--quiet", pin],
                capture_output=True, text=True, timeout=60,
            )
            if out.returncode == 0:
                status, detail = "resolvable", None
            else:
                combined = (out.stdout + out.stderr).strip()
                if "No matching distribution" in combined or "Could not find a version" in combined:
                    status = "version-not-found"
                else:
                    status = "unknown"
                detail = combined.splitlines()[-1] if combined else None
        except FileNotFoundError:
            status, detail = "unknown", "pip not found"
        except Exception as exc:
            status, detail = "unknown", str(exc)
        results.append({"pin": pin, "status": status, "detail": detail})
    return results


@env.command()
@click.argument("target", required=False, default=None)
@click.option("--dockerfile", is_flag=True, default=False, help="Emit a Dockerfile draft.")
@click.option("--cuda", "cuda_tag", default=None, metavar="TAG",
              help="With --dockerfile: nvidia/cuda:<TAG>-runtime-ubuntu22.04 base instead of python:slim.")
@click.option("--out", "out_path", default=None, type=click.Path(dir_okay=False),
              help="Write the recipe/Dockerfile to this file (atomically) instead of stdout.")
@click.option("--validate", "validate_mode", is_flag=True, default=False,
              help="Resolve every pin against PyPI WITHOUT installing; exit 15 if any pin fails.")
@click.option("--execute", "execute_mode", is_flag=True, default=False,
              help="Install the recipe's pins after showing it (asks first unless -y).")
@click.option("--target-venv", "target_venv", default=None, metavar="PATH",
              help="With --execute: create (if absent) and install into this venv, not the running interpreter.")
@click.option("--conda-env", "conda_env", default=None, metavar="NAME",
              help="With --execute: install into this conda environment via `conda run -n NAME`.")
@click.option("-y", "--yes", is_flag=True, default=False, help="Skip the execute confirmation.")
def replay(target: str | None, dockerfile: bool, cuda_tag: str | None, out_path: str | None,
          validate_mode: bool, execute_mode: bool, target_venv: str | None,
          conda_env: str | None, yes: bool) -> None:
    """Print (or execute) the reproduction recipe for TARGET (a run id, commit hash, or
    snapshot id) — or the latest local snapshot when omitted.

    Also reachable as the top-level `av replay <target>` alias (v1.2.2). TARGET
    resolution loads the snapshot from the local CAS or the registry (snapshots ride the
    normal object flow at push), so any clone can reproduce an experiment's environment
    without touching the authoring machine. On another machine, resolve by RUN id (runs
    carry env_snapshot_id server-side) or by the raw snapshot id from `.avh.replay` —
    commit-payload ids live in local commit files only.
    """
    if cuda_tag and not dockerfile:
        fail(None, "validation", "--cuda only applies together with --dockerfile.")
    if target_venv and conda_env:
        fail(None, "validation", "--target-venv and --conda-env are mutually exclusive.")

    repo_root = ensure_repo()
    snap, source = resolve_replay_target(repo_root, target)
    if snap is None:
        fail(None, "validation",
             "No snapshot found — `av env snapshot` first, or pass a run/commit whose "
             "environment was captured and pushed.")

    recipe = render_recipe(snap, dockerfile, cuda_tag=cuda_tag)
    pins = snap.get("freeze") or [f"{k}=={v}" for k, v in (snap.get("pins") or {}).items()]
    json_mode = current_output_mode() == "json"

    validation_result = None
    if validate_mode:
        validation_result = _validate_pins(pins)
        if any(r["status"] != "resolvable" for r in validation_result):
            if not json_mode:
                for r in validation_result:
                    color = "green" if r["status"] == "resolvable" else "red"
                    suffix = f"  ({r['detail']})" if r.get("detail") else ""
                    click.secho(f"  [{r['status']}] {r['pin']}{suffix}", fg=color)
            fail(None, "validation", "One or more pins failed to resolve — see the "
                 "per-pin table above (or error.data.validation in JSON mode).",
                 data={"validation": validation_result})

    if out_path:
        atomic_write_text(pathlib.Path(out_path), recipe)
    elif not json_mode:
        if source and source != "local":
            click.secho(f"# snapshot {source}", fg="cyan")
        click.echo(recipe)
        if validate_mode:
            for r in validation_result:
                click.secho(f"  [{r['status']}] {r['pin']}", fg="green")

    executed = False
    exec_target_desc = None
    if execute_mode:
        if not pins:
            fail(None, "validation", "Nothing to install — snapshot has no pins.")
        pip_prefix, exec_target_desc = _resolve_pip_invocation(target_venv, conda_env)
        if not yes and not json_mode:
            click.secho(f"About to run {len(pins)} pip install command(s) into "
                        f"{exec_target_desc}. Continue? [y/N]", fg="yellow", nl=False)
            if input().strip().lower() not in ("y", "yes"):
                click.secho("Aborted — nothing executed.", fg="yellow")
                return
        import subprocess as _sp

        for pin in pins:
            rc = _sp.call(pip_prefix + ["install", pin])
            if rc != 0:
                fail(None, "validation",
                     f"pip install failed for: {pin} (target: {exec_target_desc})")
        executed = True
        if not json_mode:
            click.secho(f"Environment reproduced into {exec_target_desc}.", fg="green")

    if json_mode:
        data = {"recipe": recipe, "snapshot": snap, "source": source or "local",
                "executed": executed, "execute_target": exec_target_desc,
                "out_path": out_path}
        if validate_mode:
            data["validation"] = validation_result
        emit_json(None, "env replay", data=data)
        return
