import datetime
import json
import os
import shutil
from pathlib import Path

from .index import Index

DATASET_EXTS = {'.csv', '.parquet', '.h5', '.arrow', '.json', '.jsonl'}
MODEL_EXTS = {'.pt', '.pth', '.safetensors', '.onnx', '.ckpt'}

AVH_VERSION = "2.0"
AVH_SCHEMA_ID = "https://aether-vault.dev/schemas/avh-2.0.json"


def _code_pointer(repo_root: Path) -> dict | None:
    """Git pointer for the code that produced this state (best-effort, never fatal).

    Aether versions ARTIFACTS; code stays in git. Capturing remote+SHA+dirty gives
    agents a reproducible link back to the source without reinventing source control.
    """
    import subprocess

    def _git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", *args], cwd=repo_root, capture_output=True,
                                 text=True, timeout=10)
            return out.stdout.strip() or None if out.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    sha = _git("rev-parse", "HEAD")
    if not sha:
        return None
    remote = _git("remote", "get-url", "origin")
    dirty = bool(_git("status", "--porcelain"))
    return {"git_remote": remote, "git_sha": sha, "dirty": dirty}


def _current_improver_id(repo_root: Path) -> str | None:
    """The locally active improver version pointer — deliberately re-read here rather
    than importing `cmd_improver.current_improver_id` (same one-line file read; avoids a
    handoff.py -> cmd_improver.py import for a single string)."""
    path = repo_root / ".av" / "improver" / "current"
    if not path.exists():
        return None
    val = path.read_text(encoding="utf-8").strip()
    return val or None


def _load_run_state(repo_root: Path) -> dict:
    path = repo_root / ".av" / "run.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _context_notes(repo_root: Path) -> list[dict]:
    """Append-only agent memory: .av/context/memory.jsonl — survives handoff regen."""
    path = repo_root / ".av" / "context" / "memory.jsonl"
    if not path.exists():
        return []
    notes = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                notes.append(json.loads(line))
            except json.JSONDecodeError:
                notes.append({"ts": None, "note": line})  # legacy free-text lines
    except OSError:
        pass
    return notes


def _commit_parent(commit: dict | None) -> str | None:
    """First parent of a commit dict, tolerating BOTH storage shapes: locally-authored
    commits store a `parents` LIST, commits fetched from the registry carry `parent_hash`
    instead."""
    if not commit:
        return None
    direct = commit.get("parent_hash")
    if direct:
        return direct
    parents = commit.get("parents")
    return parents[0] if isinstance(parents, list) and parents else None


def _metrics_history_tail(repo_root: Path, limit: int = 10) -> list[dict]:
    """Last `limit` commits' key metrics, newest first — trend without API calls."""
    branch, head_hash = resolve_head(repo_root)
    out: list[dict] = []
    cur = head_hash
    seen: set[str] = set()
    while cur and len(out) < limit and cur not in seen:
        seen.add(cur)
        commit = load_commit(repo_root, cur)
        if not commit:
            break
        if isinstance(commit.get("metrics"), dict) and commit["metrics"]:
            out.append({
                "hash": cur[:12],
                "message": (commit.get("message") or "")[:80],
                "metrics": commit["metrics"],
                "run_id": next((t.split(":", 1)[1] for t in commit.get("tags", [])
                                if t.startswith("run:")), None),
            })
        cur = _commit_parent(commit)
    return out


def build_semantic_summary(repo_root: Path, current_commit: dict | None) -> dict | None:
    """Semantic diff vs the parent commit via semdiff (layers/chunks/datasets)."""
    from .semdiff import diff_trees, human_summary

    if not current_commit:
        return None
    parent_hash = _commit_parent(current_commit)
    parent = load_commit(repo_root, parent_hash) if parent_hash else None
    sd = diff_trees((parent or {}).get("tree"), current_commit.get("tree"))
    sd["summary"] = human_summary(sd)
    return sd


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
        ref_content = ref_path.read_text().strip() if ref_path.exists() else ""
        commit_hash = ref_content or None
        return branch, commit_hash

    return "detached", head_content


def load_commit(repo_root: Path, commit_hash: str) -> dict | None:
    from .exceptions import AmbiguousCommitHash
    from .fsutil import find_commit_file

    try:
        commit_path = find_commit_file(repo_root, commit_hash)
    except (FileNotFoundError, AmbiguousCommitHash):
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

    run_state = _load_run_state(repo_root)
    run_id = run_state.get("run_id") or os.environ.get("AV_RUN_ID")
    env_path = repo_root / ".av" / "env_snapshot.json"
    replay = None
    if env_path.exists():
        try:
            replay = json.loads(env_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            replay = None
        if isinstance(replay, dict):
            # v1.2.2 env snapshot/replay: the canonical content id rides .avh.replay so
            # an agent can fetch the exact snapshot object from any registry clone
            # (`av replay <snapshot-id>` / GET /api/objects/<id>).
            from .core import env_snapshot_id

            try:
                replay["snapshot_id"] = env_snapshot_id(replay)
            except TypeError:
                pass

    return {
        "$schema": AVH_SCHEMA_ID,
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
        # --- v2: context-memory layer -------------------------------------
        "lineage": {
            "run_id": run_id,
            "parent_run_ids": run_state.get("parent_run_ids", []),
            "code_pointer": _code_pointer(repo_root),
            # The locally active improver version pointer, null when none is set --
            # `.avh` generation is local-only/offline, so this is never a network fetch.
            "improver_id": _current_improver_id(repo_root),
        },
        "semantic_summary": build_semantic_summary(repo_root, commit_data),
        "replay": replay,
        "context_memory": {
            "notes": _context_notes(repo_root),
            "metrics_history_tail": _metrics_history_tail(repo_root),
        },
    }


def upgrade_handoff(doc: dict) -> dict:
    """Upgrades a v1 .avh document to the v2 shape in memory (never mutates the file).

    v1 docs lack $schema/lineage/semantic_summary/replay/context_memory; agents reading
    old snapshots get a consistent shape with nulls instead of KeyErrors.
    """
    if str(doc.get("avh_version", "")).startswith("2"):
        return doc
    upgraded = dict(doc)
    upgraded.setdefault("$schema", AVH_SCHEMA_ID)
    upgraded["avh_version"] = AVH_VERSION
    upgraded.setdefault("lineage", {"run_id": None, "parent_run_ids": [], "code_pointer": None,
                                    "improver_id": None})
    upgraded.setdefault("semantic_summary", None)
    upgraded.setdefault("replay", None)
    upgraded.setdefault("context_memory", {"notes": [], "metrics_history_tail": []})
    return upgraded


def _validate_handoff_structural(doc: dict) -> list[str]:
    """The original hand-rolled check — no jsonschema dependency. Runtime always has
    this available (dependency-free); used as-is when jsonschema isn't importable, and
    as a defense-in-depth second opinion even when it is (see validate_handoff)."""
    problems: list[str] = []
    version = str(doc.get("avh_version", ""))
    if not version:
        problems.append("missing avh_version")
    for required in ("generated_at", "current_branch"):
        if required not in doc:
            problems.append(f"missing field: {required}")
    cm = doc.get("context_memory") or {}
    if not isinstance(cm.get("notes", []), list):
        problems.append("context_memory.notes must be a list")
    ss = doc.get("semantic_summary")
    if ss is not None and not isinstance(ss, dict):
        problems.append("semantic_summary must be an object or null")
    lin = doc.get("lineage") or {}
    for key in ("run_id", "parent_run_ids", "code_pointer"):
        if key not in lin:
            problems.append(f"lineage missing: {key}")
    return problems


def validate_handoff(doc: dict) -> list[str]:
    """Validates a `.avh` v2 document. Returns a problem list (empty = valid). Uses real
    jsonschema.validate() when jsonschema is importable, falling back to the hand-rolled
    structural check when it isn't (jsonschema is never a hard requirement to use av).
    The structural check also always runs as a second opinion."""
    try:
        import jsonschema

        from .core import load_contract_schema

        schema = load_contract_schema("avh-2.0")
        validator = jsonschema.Draft202012Validator(schema)
        problems = [f"{'.'.join(str(p) for p in err.path) or '(root)'}: {err.message}"
                   for err in sorted(validator.iter_errors(doc), key=str)]
    except ImportError:
        problems = []
    return problems + [p for p in _validate_handoff_structural(doc) if p not in problems]


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
    with_memory: bool = True,
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
    if not with_memory:
        # Privacy/size trim for agents that only want the state snapshot.
        handoff_data["context_memory"] = {"notes": [], "metrics_history_tail": []}

    # Validate on every write, not just when `av context validate` is invoked by hand --
    # a violation here means build_handoff_dict() itself produced a malformed document.
    problems = validate_handoff(handoff_data)
    if problems:
        raise ValidationError(
            "Generated .avh document failed validation (this is a bug in "
            "build_handoff_dict, not your input) — " + "; ".join(problems)
        )

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
