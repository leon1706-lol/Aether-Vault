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

    snap: dict = {
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
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
def replay(target: str | None, dockerfile: bool) -> None:
    """Print the reproduction recipe for the latest (or given) snapshot."""
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

    if current_output_mode() == "json":
        emit_json(None, "env replay", data={"recipe": recipe, "snapshot": snap})
        return
    click.echo(recipe)
