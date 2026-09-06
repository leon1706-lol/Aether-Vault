"""Append-only agent action log (v1.3.1). `.av/actions.jsonl` records one line per logged
decision, content-addressed via `casobj` so it can be referenced from a commit/run and
replayed with `av replay-actions`.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path


def _log_path(repo_root: Path) -> Path:
    return repo_root / ".av" / "actions.jsonl"


def log_action(repo_root: Path, action: str, details: dict | None = None,
              actor: str | None = None, command: list[str] | None = None) -> dict:
    """Appends one entry. `command`, when given, is what `av replay-actions --execute`
    re-runs to check reproducibility."""
    import os

    entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "actor": actor or os.environ.get("AV_AUTHOR", "anonymous"),
        "action": action,
        "details": details or {},
        "command": command,
    }
    path = _log_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_actions(repo_root: Path) -> list[dict]:
    path = _log_path(repo_root)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # tolerate a corrupt/partial trailing line, same as context/memory.jsonl
    return out


def publish_action_log(repo_root: Path, client, project_id: str, run_id: str | None = None) -> str | None:
    """Content-addresses the current action log and uploads it, returning the object id
    (or None if the log is empty). Never raises — this is a nice-to-have record, not a gate."""
    from . import casobj

    actions = read_actions(repo_root)
    if not actions:
        return None
    doc = {"kind": "action_log", "actions": actions}
    object_id = casobj.write_object(repo_root, doc)
    if not client.upload_object(casobj.object_path(repo_root, object_id), object_id):
        return None
    resp = client.session.post(f"{client.server_url}/api/action-logs", json={
        "project_id": project_id, "run_id": run_id, "object_id": object_id,
    })
    return object_id if resp.status_code in (200, 201) else None
