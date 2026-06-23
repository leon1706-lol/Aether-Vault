import datetime
import json
import shutil
from pathlib import Path

from .index import Index

DATASET_EXTS = {'.csv', '.parquet', '.h5', '.arrow', '.json', '.jsonl'}
MODEL_EXTS = {'.pt', '.pth', '.safetensors', '.onnx', '.ckpt'}

AVH_VERSION = "1.0"


def classify_lineage(rel_path: str) -> str:
    ext = Path(rel_path).suffix.lower()
    if ext in MODEL_EXTS:
        return "model"
    if ext in DATASET_EXTS:
        return "dataset"
    return "code"


def resolve_head(repo_root: Path) -> tuple[str, str | None]:
    """Returns (branch_name_or_'detached', current_commit_hash_or_None)."""
    head_path = repo_root / ".av" / "HEAD"
    if not head_path.exists():
        return "detached", None

    head_content = head_path.read_text().strip()
    if head_content.startswith("ref: "):
        branch = head_content.split("/")[-1] if head_content.startswith("ref: refs/heads/") else "detached"
        ref_path = repo_root / ".av" / head_content.split(": ", 1)[1]
        commit_hash = ref_path.read_text().strip() if ref_path.exists() and ref_path.read_text().strip() else None
        return branch, commit_hash

    return "detached", head_content


def load_commit(repo_root: Path, commit_hash: str) -> dict | None:
    commit_path = repo_root / ".av" / "commits" / f"{commit_hash}.json"
    if not commit_path.exists():
        return None
    with open(commit_path, "r") as f:
        return json.load(f)


def build_handoff_dict(repo_root: Path, agent_instructions: str | None) -> dict:
    branch, commit_hash = resolve_head(repo_root)
    commit_data = load_commit(repo_root, commit_hash) if commit_hash else None

    idx = Index(repo_root)
    model_paths, dataset_lineage, code_files = [], [], []
    for rel_path, entry in idx.get_all_entries().items():
        lineage = classify_lineage(rel_path)
        if lineage == "model":
            model_paths.append({
                "rel_path": rel_path,
                "hash": entry["hash"],
                "size": entry["size"],
                "layers": entry.get("layers", []),
            })
        elif lineage == "dataset":
            dataset_lineage.append({
                "rel_path": rel_path,
                "hash": entry["hash"],
                "size": entry["size"],
            })
        else:
            code_files.append(rel_path)

    return {
        "avh_version": AVH_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "current_branch": branch,
        "current_commit_hash": commit_hash,
        "commit_message": commit_data.get("message") if commit_data else None,
        "tags": commit_data.get("tags", []) if commit_data else [],
        "metrics": commit_data.get("metrics", {}) if commit_data else {},
        "model_paths": sorted(model_paths, key=lambda e: e["rel_path"]),
        "dataset_lineage": sorted(dataset_lineage, key=lambda e: e["rel_path"]),
        "code_files": sorted(code_files),
        "agent_instructions": agent_instructions,
    }


def diff_model_weights(repo_root: Path, current_tree: list[dict], parent_commit_hash: str | None) -> list[dict]:
    """Compares model files in current_tree against the parent commit's tree.

    For safetensors-style entries with per-layer hashes, reports exactly which
    layers changed. For plain model files (no layer split), only reports
    whether the whole-file hash changed.
    """
    parent_tree = {}
    if parent_commit_hash:
        parent_commit = load_commit(repo_root, parent_commit_hash)
        if parent_commit:
            parent_tree = parent_commit.get("tree", {})

    results = []
    for entry in current_tree:
        rel_path = entry["rel_path"]
        parent_entry = parent_tree.get(rel_path)

        if not parent_entry:
            results.append({"rel_path": rel_path, "status": "new", "changed_layers": [], "changed_pct": 1.0})
            continue

        layers = entry.get("layers") or []
        parent_layers = parent_entry.get("layers") or []
        if layers and parent_layers:
            parent_layer_hashes = {l["name"]: l["hash"] for l in parent_layers}
            changed = [l["name"] for l in layers if parent_layer_hashes.get(l["name"]) != l["hash"]]
            total = len(layers) or 1
            results.append({
                "rel_path": rel_path,
                "status": "changed" if changed else "unchanged",
                "changed_layers": changed,
                "unchanged_layers": len(layers) - len(changed),
                "changed_pct": len(changed) / total,
            })
        else:
            changed = entry["hash"] != parent_entry.get("hash")
            results.append({
                "rel_path": rel_path,
                "status": "changed" if changed else "unchanged",
                "changed_layers": [],
                "changed_pct": 1.0 if changed else 0.0,
            })

    return results


def init_handoff_dir(repo_root: Path) -> Path:
    vault_dir = repo_root / "Aether-Handoff"
    (vault_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    (vault_dir / "weight-diffs").mkdir(parents=True, exist_ok=True)
    hub_path = vault_dir / "Handoff-Hub.md"
    if not hub_path.exists():
        hub_path.write_text("# Aether-Handoff Hub\n\nChronological index of all handoff snapshots.\n\n", encoding="utf-8")
    return vault_dir


def write_handoff_note(vault_dir: Path, handoff_data: dict, snapshot_id: str, weight_diff: list[dict] | None) -> Path:
    out_path = vault_dir / "snapshots" / f"{snapshot_id}.md"
    lines = [
        f"# Handoff Snapshot — {snapshot_id}\n\n",
        f"- **Branch:** {handoff_data['current_branch']}\n",
        f"- **Commit:** {handoff_data['current_commit_hash']}\n",
        f"- **Generated at:** {handoff_data['generated_at']}\n",
        f"- **Message:** {handoff_data['commit_message'] or '—'}\n\n",
    ]

    lines.append("## Tags & Metrics\n\n")
    lines.append(f"- **Tags:** {', '.join(handoff_data['tags']) or '—'}\n")
    if handoff_data["metrics"]:
        for k, v in sorted(handoff_data["metrics"].items()):
            lines.append(f"- **{k}:** {v}\n")
    else:
        lines.append("- **Metrics:** —\n")
    lines.append("\n")

    lines.append("## Model Paths\n\n")
    if handoff_data["model_paths"]:
        for m in handoff_data["model_paths"]:
            lines.append(f"- `{m['rel_path']}` — {m['size']} bytes — `{m['hash'][:12]}`\n")
    else:
        lines.append("- —\n")
    lines.append("\n")

    lines.append("## Dataset Lineage\n\n")
    if handoff_data["dataset_lineage"]:
        for d in handoff_data["dataset_lineage"]:
            lines.append(f"- `{d['rel_path']}` — {d['size']} bytes — `{d['hash'][:12]}`\n")
    else:
        lines.append("- —\n")
    lines.append("\n")

    if weight_diff is not None:
        lines.append("## Weight Changes\n\n")
        if weight_diff:
            for w in weight_diff:
                if w["changed_layers"]:
                    lines.append(f"- `{w['rel_path']}` — {w['status']} — changed layers: {', '.join(w['changed_layers'])}\n")
                else:
                    lines.append(f"- `{w['rel_path']}` — {w['status']}\n")
        else:
            lines.append("- No model files tracked.\n")
        lines.append("\n")

    lines.append("## Agent Instructions\n\n")
    lines.append(f"{handoff_data['agent_instructions'] or '—'}\n")

    out_path.write_text("".join(lines), encoding="utf-8")
    return out_path


def write_handoff_hub(vault_dir: Path) -> Path:
    snapshots_dir = vault_dir / "snapshots"
    entries = sorted(snapshots_dir.glob("*.md"), reverse=True)

    lines = ["# Aether-Handoff Hub\n\n", "Chronological index of all handoff snapshots (newest first).\n\n"]
    for entry in entries:
        lines.append(f"- [{entry.stem}](snapshots/{entry.name})\n")

    hub_path = vault_dir / "Handoff-Hub.md"
    hub_path.write_text("".join(lines), encoding="utf-8")
    return hub_path


def generate_handoff(
    repo_root: Path,
    update: bool = False,
    agent_instructions: str | None = None,
    diff_weights: bool = False,
    since: str | None = None,
) -> tuple[Path, Path]:
    from .exceptions import ValidationError

    avh_path = repo_root / "handoff.avh"
    if avh_path.exists() and not update:
        raise ValidationError(
            f"{avh_path.name} already exists. Use `av handoff --update` to refresh it."
        )

    if avh_path.exists() and update and agent_instructions is None:
        with open(avh_path, "r") as f:
            agent_instructions = json.load(f).get("agent_instructions")

    handoff_data = build_handoff_dict(repo_root, agent_instructions)

    with open(avh_path, "w") as f:
        json.dump(handoff_data, f, indent=2, sort_keys=True)

    vault_dir = init_handoff_dir(repo_root)

    weight_diff = None
    if diff_weights:
        parent_hash = since
        if parent_hash is None:
            commit_data = load_commit(repo_root, handoff_data["current_commit_hash"]) if handoff_data["current_commit_hash"] else None
            parents = commit_data.get("parents", []) if commit_data else []
            parent_hash = parents[0] if parents else None
        weight_diff = diff_model_weights(repo_root, handoff_data["model_paths"], parent_hash)

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    short_hash = (handoff_data["current_commit_hash"] or "nocommit")[:7]
    snapshot_id = f"{timestamp}_{short_hash}"

    md_path = write_handoff_note(vault_dir, handoff_data, snapshot_id, weight_diff)
    shutil.copy2(avh_path, vault_dir / "snapshots" / f"{snapshot_id}.avh")
    shutil.copy2(avh_path, vault_dir / "latest.avh")
    write_handoff_hub(vault_dir)

    return avh_path, md_path
