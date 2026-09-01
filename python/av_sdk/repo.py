"""av_sdk.Repo — internal-seam implementation (v1.2.1).

Calls the project's internal functions DIRECTLY (Index, compute_status, stage_one_file,
commit_staged, flush_pending_push, semdiff, handoff builder) instead of shelling through
the click app: no chdir, no stdout capture, no output parsing. The single-writer
invariant is preserved because commits funnel through core.commit_staged →
_finalize_commit — the exact path the CLI uses.

Return values mirror the CLI's `--output json` data payloads so agents can switch
between the two surfaces freely.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from pathlib import Path

from av_cli.exceptions import AetherVaultException

from .exceptions import SDKError


class Repo:
    """Agent-facing handle for one Aether-Vault repository."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        if not (self.path / ".av").is_dir():
            raise SDKError("not_a_repo",
                           f"{self.path} is not an Aether-Vault repository (no .av/).")

    def __enter__(self) -> "Repo":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    # -- internal helpers -----------------------------------------------------

    def _index(self):
        from av_cli.index import Index

        return Index(self.path)

    def _cfg(self) -> dict:
        from av_cli.core import load_config

        return load_config(self.path)

    def _client(self):
        from av_cli.client import VaultClient

        cfg = self._cfg()
        return VaultClient(cfg.get("remote_url", "http://localhost:8000"),
                           cfg.get("remote_api_token"))

    def _run_id(self) -> str | None:
        from av_cli.cmd_run import current_run_id

        return current_run_id(self.path)

    def _head(self):
        from av_cli.handoff import resolve_head

        return resolve_head(self.path)

    @staticmethod
    def _fail(code: str, message: str):
        raise SDKError(code, message)

    @staticmethod
    def _wrap_validation(fn):
        try:
            return fn()
        except SDKError:
            raise
        except AetherVaultException as exc:
            raise SDKError("validation", str(exc)) from None

    # -- status ---------------------------------------------------------------

    def status(self) -> dict:
        from av_cli.core import compute_status

        idx = self._index()
        branch = self._head()[0]
        staged, modified, deleted, untracked = compute_status(self.path, idx)
        return {"branch": branch, "staged": sorted(staged), "modified": sorted(modified),
                "deleted": sorted(deleted), "untracked": sorted(untracked)}

    # -- add ------------------------------------------------------------------

    def add(self, *paths: str | Path) -> dict:
        from av_cli import attributes as attr_mod
        from av_cli.core import iter_working_files, stage_one_file

        cfg = self._cfg()
        threshold = cfg.get("lfs_threshold_mb", 50) * 1024 * 1024
        idx = self._index()
        rules = attr_mod.load_attributes(self.path)

        files: list[Path] = []
        for p in paths:
            po = Path(p)
            if not po.is_absolute():
                # Relative paths resolve against THE REPO, not the agent's cwd —
                # the whole point of the SDK is that callers never chdir.
                po = self.path / po
            po = po.resolve()
            if po.is_file():
                files.append(po)
            elif po.is_dir():
                from av_cli.core import iter_working_files

                files.extend(iter_working_files(po))

        staged: list[dict] = []
        changed = False
        for fpath in files:
            rel = str(fpath.relative_to(self.path)).replace(os.sep, "/")
            if rel.endswith(".av-pointer"):
                continue
            if stage_one_file(self.path, idx, threshold, fpath, rel,
                              attr_mod.flags_for(rules, rel)):
                changed = True
                e = idx.get_entry(rel) or {}
                staged.append({"path": rel, "type": e.get("type", "file"),
                               "hash": e.get("hash"), "size": e.get("size")})
        if changed:
            idx.save()
        return {"staged": staged, "count": len(staged)}

    # -- commit ---------------------------------------------------------------

    def commit(
        self,
        message: str,
        *,
        tags: list[str] | tuple = (),
        metrics: dict | None = None,
        no_upload: bool = False,
    ) -> dict:
        from av_cli.core import commit_staged

        idx = self._index()
        if not idx.get_staged_entries():
            self._fail("nothing_to_commit", "Nothing staged to commit.")

        run_id = self._run_id()
        all_tags = tuple(tags)
        if run_id and f"run:{run_id}" not in all_tags:
            all_tags = all_tags + (f"run:{run_id}",)

        sink: dict = {}

        def sink_cb(result):
            sink.update(result)

        head_hash = commit_staged(
            self.path, message, tags=all_tags, metrics=metrics or {},
            run_id=run_id, defer_upload=no_upload, result_sink=sink_cb,
        )
        return {
            "committed": True,
            "hash": head_hash,
            "short": (head_hash or "")[:7],
            "message": message,
            "tags": list(all_tags),
            "metrics": metrics or {},
            "run_id": run_id,
            "queued": bool(sink.get("queued")) or bool(no_upload),
            "queued_reason": sink.get("queued_reason")
                             or ("upload_deferred" if no_upload else None),
        }

    # -- push -----------------------------------------------------------------

    def push(self) -> dict:
        from av_cli.core import flush_pending_push, load_pending_push

        client = self._client()
        pending = load_pending_push(self.path)
        if not pending:
            reachable = None
            try:
                reachable = bool(client.server_available())
            except Exception:
                reachable = None
            return {"drained": 0, "still_queued": 0, "reachable": reachable}
        still = flush_pending_push(self.path, client)
        return {"drained": len(pending) - len(still),
                "still_queued": len(still),
                "reachable": True}

    # -- log ------------------------------------------------------------------

    def log(self, limit: int = 30, branch: str | None = None) -> list[dict]:
        """Machine-readable first-parent walk from the tip (newest first)."""
        from av_cli.handoff import load_commit

        if branch:
            ref = self.path / ".av" / "refs" / "heads" / branch
            cur = ref.read_text().strip() if ref.exists() else None
        else:
            cur = self._head()[1]

        out: list[dict] = []
        seen: set[str] = set()
        while cur and len(out) < limit and cur not in seen:
            seen.add(cur)
            c = load_commit(self.path, cur)
            if not c:
                break
            extras = c.get("extra_parents")
            parents = [c["parent_hash"]] if c.get("parent_hash") else []
            if extras:
                parents.extend(json.loads(extras))
            out.append({
                "hash": cur,
                "short": cur[:7],
                "message": c.get("message"),
                "author": c.get("author"),
                "tags": c.get("tags") or [],
                "metrics": c.get("metrics") or {},
                "parents": parents,
            })
            cur = c.get("parent_hash")
        return out

    # -- diff -----------------------------------------------------------------

    def diff_semantic(self, target: str | None = None) -> dict:
        from av_cli.handoff import load_commit
        from av_cli.semdiff import diff_trees, human_summary

        head_hash = self._head()[1]
        head_commit = load_commit(self.path, head_hash) if head_hash else None
        if target:
            tgt = load_commit(self.path, target)
            parent_hash = (tgt or {}).get("parent_hash")
            base = load_commit(self.path, parent_hash) if target and parent_hash else None
            old_tree = (base or {}).get("tree", {})
            new_tree = (tgt or {}).get("tree", {})
            base_hash = (base or {}).get("hash")
            new_hash = (tgt or {}).get("hash")
        else:
            parent_hash = (head_commit or {}).get("parent_hash")
            base = load_commit(self.path, parent_hash) if parent_hash else None
            old_tree = (base or {}).get("tree", {})
            new_tree = (head_commit or {}).get("tree", {})
            base_hash = (base or {}).get("hash")
            new_hash = head_hash
        sd = diff_trees(old_tree, new_tree)
        sd["base"] = base_hash
        sd["target"] = new_hash
        sd["summary"] = human_summary(sd)
        return sd

    # -- runs -----------------------------------------------------------------

    def run_start(self, name: str | None = None, parent_run_id: str | None = None) -> dict:
        from av_cli.cmd_run import _register_remote, _state_path

        run_id = str(uuid.uuid4())
        registered, _resp = _register_remote(
            self.path,
            {"id": run_id, "name": name, "parent_run_id": parent_run_id},
        )
        state = {"run_id": run_id, "name": name, "status": "running",
                 "parent_run_ids": [parent_run_id] if parent_run_id else [],
                 "code_pointer": None}
        _state_path(self.path).write_text(json.dumps(state), encoding="utf-8")
        return {"run_id": run_id, "name": name, "registered_server_side": registered}

    def run_finish(self, *, failed: bool = False, metrics: dict | None = None) -> dict:
        from av_cli.cmd_run import _state_path

        path = _state_path(self.path)
        if not path.exists():
            self._fail("validation", "No active run — run_start() first.")
        state = json.loads(path.read_text(encoding="utf-8"))
        run_id = state.get("run_id")

        endpoint = f"/api/runs/{run_id}/{'fail' if failed else 'complete'}"
        delivered = False
        try:
            client = self._client()
            resp = client.session.post(f"{client.server_url}{endpoint}",
                                       json={"metrics_summary": metrics or {}},
                                       timeout=30)
            delivered = resp.status_code == 200
        except Exception:
            pass
        path.unlink(missing_ok=True)
        return {"run_id": run_id,
                "status": "failed" if failed else "completed",
                "metrics_summary": metrics or {},
                "delivered_to_registry": delivered}

    # -- context memory -------------------------------------------------------

    def context_note(self, note: str, agent: str | None = None) -> dict:
        mem_dir = self.path / ".av" / "context"
        mem_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                 "agent": agent or os.environ.get("AV_AUTHOR", "anonymous"),
                 "note": note}
        with open(mem_dir / "memory.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return {"appended": True, "entry": entry}

    def handoff_dict(self) -> dict:
        from av_cli.handoff import build_handoff_dict

        return build_handoff_dict(self.path, None)

    def publish_handoff(self) -> dict:
        """v1.2.5: generates/updates handoff.avh and links it to the active run so the
        WebUI run-detail view can render context-memory notes — the SDK counterpart to
        `av handoff --publish`. OPT-IN and explicit only: notes can hold private
        reasoning, so nothing publishes them without calling this. Requires an active
        run (run_start() / AV_RUN_ID) and a reachable registry — raises SDKError
        ("validation") for either failure rather than queuing (no offline-retry queue
        exists for run-level metadata, unlike commits)."""
        from av_cli.core import hash_file_safe
        from av_cli.handoff import generate_handoff

        run_id = self._run_id()
        if not run_id:
            self._fail("validation", "No active run (run_start() / AV_RUN_ID) — "
                       "publish_handoff() links the .avh to a run.")

        avh_path, _md_path = generate_handoff(self.path, update=True, agent_instructions=None)

        client = self._client()
        if not client.server_available():
            self._fail("validation", f"Registry unreachable at {client.server_url} — "
                       "could not publish.")
        avh_hash = hash_file_safe(str(avh_path))
        if not client.upload_object(avh_path, avh_hash):
            self._fail("validation", "Failed to upload the .avh object to the registry.")
        resp = client.session.post(f"{client.server_url}/api/runs/{run_id}/avh",
                                   json={"avh_object_id": avh_hash}, timeout=30)
        if resp.status_code != 200:
            self._fail("validation",
                       f"Failed to link the .avh to run {run_id}: HTTP {resp.status_code}")
        return {"run_id": run_id, "avh_object_id": avh_hash, "path": str(avh_path)}
