"""av env — environment snapshot & replay recipes (v1.2.0).

Recipe-exact, not bit-exact: captures the interpreter + curated package pins + seeds
into .av/env_snapshot.json (embedded into .avh replay), and renders reproduction
instructions. Full pip freeze via --full for maximal fidelity.
"""

import datetime
import json
import os
import pathlib
import sys

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json

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


@click.group()
def env() -> None:
    """Environment snapshots and replay recipes for runs/commits."""


@env.command()
@click.option("--full", is_flag=True, default=False, help="Include a full pip freeze.")
def snapshot(full: bool) -> None:
    """Capture the current environment into the repo (.av/env_snapshot.json)."""
    repo_root = ensure_repo()
    snap = _collect(full)
    out = repo_root / ".av" / "env_snapshot.json"
    atomic_write_text(out, json.dumps(snap, indent=2, sort_keys=True))
    if current_output_mode() == "json":
        emit_json(None, "env snapshot", data={"path": str(out), "pins": snap["pins"],
                                              "full": bool(snap.get("freeze"))})
        return
    click.secho(f"Snapshot written ({len(snap['pins'])} curated pins"
                f"{', full freeze' if snap.get('freeze') else ''}).", fg="green")


@env.command()
@click.argument("target", required=False, default=None)
@click.option("--dockerfile", is_flag=True, default=False, help="Emit a Dockerfile draft.")
@click.option("--execute", "execute_mode", is_flag=True, default=False,
              help="Execute the recipe's pip installs after showing it (asks first unless -y).")
@click.option("-y", "--yes", is_flag=True, default=False, help="Skip the execute confirmation.")
def replay(target: str | None, dockerfile: bool, execute_mode: bool, yes: bool) -> None:
    """Print (or execute) the reproduction recipe for the latest (or given) snapshot."""
    repo_root = ensure_repo()
    path = repo_root / ".av" / "env_snapshot.json"
    if not path.exists():
        fail(None, "validation", "No snapshot — `av env snapshot` first.")
    snap = json.loads(path.read_text(encoding="utf-8"))
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
                                            "executed": bool(execute_mode)})
        return
