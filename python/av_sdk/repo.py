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

from .exceptions import SDKError, error_from_code


class Repo:
    """Agent-facing handle for one Aether-Vault repository."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        if not (self.path / ".av").is_dir():
            raise error_from_code("not_a_repo",
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
        from av_cli.core import resolve_remote

        return VaultClient(*resolve_remote(self.path, self._cfg()))

    def _run_id(self) -> str | None:
        from av_cli.cmd_run import current_run_id

        return current_run_id(self.path)

    def _head(self):
        from av_cli.handoff import resolve_head

        return resolve_head(self.path)

    @staticmethod
    def _fail(code: str, message: str):
        raise error_from_code(code, message)

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
            "ref_race": sink.get("ref_race"),
        }

    # -- push -----------------------------------------------------------------

    def push(self) -> dict:
        from av_cli.core import flush_pending_push, load_pending_push

        client = self._client()
        pending = load_pending_push(self.path)
        if not pending:
            # Matches cmd_history.py::push()'s own "nothing pending" branch -- reachability
            # is genuinely UNKNOWN here (never checked), not False.
            return {"drained": 0, "still_queued": 0, "reachable": None}
        if not client.server_available():
            return {"drained": 0, "still_queued": len(pending), "reachable": False}
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
            # Local commit JSON stores a single "parents" LIST -- "parent_hash"/
            # "extra_parents" are av_server's DB *column* names, a different schema.
            parents = c.get("parents") or []
            out.append({
                "hash": cur,
                "short": cur[:7],
                "message": c.get("message"),
                "author": c.get("author"),
                "tags": c.get("tags") or [],
                "metrics": c.get("metrics") or {},
                "parents": parents,
            })
            # First-parent walk, same rule as history.py::walk_history().
            cur = parents[0] if parents else None
        return out

    # -- diff -----------------------------------------------------------------

    def diff_semantic(self, target: str | None = None) -> dict:
        from av_cli.handoff import _commit_parent, load_commit
        from av_cli.semdiff import diff_trees, human_summary

        # `_commit_parent()` tolerates both storage shapes (local `parents` list vs.
        # registry `parent_hash`).
        head_hash = self._head()[1]
        head_commit = load_commit(self.path, head_hash) if head_hash else None
        if target:
            tgt = load_commit(self.path, target)
            parent_hash = _commit_parent(tgt)
            base = load_commit(self.path, parent_hash) if target and parent_hash else None
            old_tree = (base or {}).get("tree", {})
            new_tree = (tgt or {}).get("tree", {})
            base_hash = (base or {}).get("hash")
            new_hash = (tgt or {}).get("hash")
        else:
            parent_hash = _commit_parent(head_commit)
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

    def run_start(self, name: str | None = None, parent_run_id: str | None = None, *,
                  kind: str = "train", improver_id: str | None = None) -> dict:
        from av_cli.cmd_run import _register_remote, _state_path
        from av_cli.core import capture_code_pointer

        run_id = str(uuid.uuid4())
        code_pointer = capture_code_pointer(self.path)
        registered, _resp = _register_remote(
            self.path,
            {"id": run_id, "project_id": self._cfg()["project_id"], "name": name,
             "parent_run_id": parent_run_id, "code_pointer": code_pointer,
             "kind": kind, "improver_id": improver_id},
        )
        state = {"run_id": run_id, "name": name, "status": "running",
                 "parent_run_ids": [parent_run_id] if parent_run_id else [],
                 "code_pointer": code_pointer, "kind": kind, "improver_id": improver_id}
        _state_path(self.path).write_text(json.dumps(state), encoding="utf-8")
        return {"run_id": run_id, "name": name, "kind": kind,
                "registered_server_side": registered}

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
                 "run_id": self._run_id(),
                 "note": note}
        with open(mem_dir / "memory.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return {"appended": True, "entry": entry}

    def handoff_dict(self) -> dict:
        from av_cli.handoff import build_handoff_dict, validate_handoff

        doc = build_handoff_dict(self.path, None)
        # Validate on this read path too, same guarantee as `av context export`/`av handoff`.
        problems = validate_handoff(doc)
        if problems:
            self._fail("validation",
                       "The freshly built .avh document failed validation (this is a "
                       "bug in build_handoff_dict, not your input) — " + "; ".join(problems))
        return doc

    def publish_handoff(self) -> dict:
        """Generates/updates handoff.avh and links it to the active run -- the SDK
        counterpart to `av handoff --publish`. Opt-in and explicit only. Requires an
        active run and a reachable registry; raises SDKError("validation") for either
        failure rather than queuing."""
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

    # -- RSI surfaces (v1.3.1) ------------------------------------------
    #
    # Scope note: every WRITE operation an autonomous loop needs to act on its own
    # self-improvement cycle is here, each reusing the same plain (non-click), side-effect-
    # free DATA functions the CLI commands call -- the same single-code-path principle
    # `commit_staged()` established for the substrate.
    #
    # Deliberately NOT reused as-is: any cmd_*.py helper whose own error path calls the
    # CLI's `fail()` -- that raises a bare `SystemExit`, wrong for a library call, which
    # must raise a catchable `SDKError` instead. `freeze_set()`/`improver_review()`/the
    # internal transition inside `improver_apply()` reimplement that one step inline.
    #
    # Pure list/search/show-many endpoints are deliberately NOT mirrored here -- one GET
    # each with no decision logic, no more discoverable via the SDK than `av ... list`
    # itself; reach them via `self._client()` or the CLI instead.

    def _online_client(self):
        client = self._client()
        if not client.server_available():
            self._fail("unreachable_queued",
                       f"Registry unreachable at {client.server_url} — this RSI surface "
                       "is server-authoritative (no offline queue for this artifact type).")
        return client

    def _freeze_guard(self) -> None:
        from av_cli.cmd_freeze import project_frozen

        frozen, reason = project_frozen(self.path)
        if frozen:
            self._fail("frozen",
                       f"Project is frozen ({reason or 'no reason given'}) — promotions "
                       "and self-edits are paused. improver_rollback() and "
                       "freeze_set(False) still work.")

    def _transition_change_set(self, client, cs_id: str, new_status: str) -> dict:
        resp = client.session.post(f"{client.server_url}/api/change-sets/{cs_id}/status",
                                   json={"status": new_status})
        if resp.status_code != 200:
            self._fail("validation",
                       f"Cannot transition change set {cs_id} to {new_status!r}: {resp.text[:200]}")
        return resp.json()

    # -- improver ---------------------------------------------------------------

    def improver_register(self, *, code_paths=(), prompt_paths=(), tool_schema_paths=(),
                          parent_id: str | None = None, policy_pack_id: str | None = None,
                          sign: bool = True) -> dict:
        from av_cli import casobj
        from av_cli.cmd_improver import _hash_paths, _set_current, current_improver_id

        self._freeze_guard()
        client = self._online_client()
        parent_id = parent_id or current_improver_id(self.path)
        manifest = {
            "kind": "improver_manifest", "manifest_version": "1.0", "parent_id": parent_id,
            "code": _hash_paths(self.path, tuple(code_paths)),
            "prompts": _hash_paths(self.path, tuple(prompt_paths)),
            "tool_schemas": _hash_paths(self.path, tuple(tool_schema_paths)),
            "policy_pack_id": policy_pack_id,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        if sign:
            sig = casobj.sign_object(manifest, self.path)
            if sig:
                manifest["signature"] = sig
        manifest_id = casobj.write_object(self.path, manifest)
        if not client.upload_object(casobj.object_path(self.path, manifest_id), manifest_id):
            self._fail("unreachable_queued", "Failed to upload the improver manifest object.")

        new_id = str(uuid.uuid4())
        resp = client.session.post(f"{client.server_url}/api/improvers", json={
            "id": new_id, "project_id": self._cfg()["project_id"],
            "manifest_object_id": manifest_id, "parent_id": parent_id,
        })
        if resp.status_code not in (200, 201):
            self._fail("validation", f"Registry rejected the improver version: {resp.text[:200]}")
        improver_id = resp.json().get("id", new_id)
        _set_current(self.path, improver_id)
        return {"id": improver_id, "manifest_object_id": manifest_id, "parent_id": parent_id,
                "signed": "signature" in manifest}

    def improver_current(self) -> str | None:
        from av_cli.cmd_improver import current_improver_id

        return current_improver_id(self.path)

    def improver_use(self, improver_id: str) -> dict:
        from av_cli.cmd_improver import _set_current

        _set_current(self.path, improver_id)
        return {"id": improver_id}

    def improver_show(self, improver_id: str) -> dict:
        from av_cli import casobj

        client = self._online_client()
        resp = client.session.get(f"{client.server_url}/api/improvers/{improver_id}")
        if resp.status_code != 200:
            self._fail("validation", f"Unknown improver version: {improver_id}")
        row = resp.json()
        manifest = casobj.read_object(self.path, row["manifest_object_id"])
        if manifest is None and client.download_object(
                row["manifest_object_id"], casobj.object_path(self.path, row["manifest_object_id"])):
            manifest = casobj.read_object(self.path, row["manifest_object_id"])
        return {**row, "manifest": manifest}

    def improver_lineage(self, improver_id: str, *, depth: int = 50) -> dict:
        client = self._online_client()
        resp = client.session.get(f"{client.server_url}/api/improvers/{improver_id}/lineage",
                                  params={"depth": depth})
        if resp.status_code != 200:
            self._fail("validation", f"Unknown improver version: {improver_id}")
        return resp.json()

    def improver_propose(self, diff_text: str, rationale: str, *, risk: str = "low",
                         improver_id: str | None = None) -> dict:
        from av_cli import casobj
        from av_cli.cmd_improver import current_improver_id

        self._freeze_guard()
        client = self._online_client()
        improver_id = improver_id or current_improver_id(self.path)
        doc = {"kind": "change_set", "improver_id": improver_id, "diff": diff_text,
              "rationale": rationale, "risk": risk,
              "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        sig = casobj.sign_object(doc, self.path)
        if sig:
            doc["signature"] = sig
        object_id = casobj.write_object(self.path, doc)
        if not client.upload_object(casobj.object_path(self.path, object_id), object_id):
            self._fail("unreachable_queued", "Failed to upload the change-set object.")
        cs_id = str(uuid.uuid4())
        resp = client.session.post(f"{client.server_url}/api/change-sets", json={
            "id": cs_id, "project_id": self._cfg()["project_id"], "improver_id": improver_id,
            "object_id": object_id, "risk": risk,
        })
        if resp.status_code not in (200, 201):
            self._fail("validation", f"Registry rejected the proposal: {resp.text[:200]}")
        return {"id": resp.json().get("id", cs_id), "improver_id": improver_id,
                "risk": risk, "object_id": object_id}

    def improver_review(self, change_set_id: str, decision: str) -> dict:
        """`decision` is `"approved"`/`"rejected"` — transitions the CHANGE SET (required
        before `improver_apply()`); distinct from `review_submit()` below, which is the
        reviewer-GATE approval `improver_promote()`'s `require_review` policy checks."""
        client = self._online_client()
        return self._transition_change_set(client, change_set_id, decision)

    def improver_apply(self, change_set_id: str) -> dict:
        from av_cli.cmd_improver import (_empty_manifest_object_id, _improver_dir,
                                         _parent_manifest_object_id, _set_current,
                                         current_improver_id)

        self._freeze_guard()
        client = self._online_client()
        resp = client.session.get(f"{client.server_url}/api/change-sets/{change_set_id}")
        if resp.status_code != 200:
            self._fail("validation", f"Unknown change set: {change_set_id}")
        cs = resp.json()
        if cs["status"] != "approved":
            self._fail("validation",
                       f"Change set {change_set_id} is '{cs['status']}', not 'approved' — "
                       "call improver_review(change_set_id, 'approved') first.")
        previous = current_improver_id(self.path)
        new_id = str(uuid.uuid4())
        create_resp = client.session.post(f"{client.server_url}/api/improvers", json={
            "id": new_id, "project_id": self._cfg()["project_id"],
            "manifest_object_id": _parent_manifest_object_id(client, cs.get("improver_id"))
                                  or _empty_manifest_object_id(self.path, client),
            "parent_id": cs.get("improver_id"),
        })
        if create_resp.status_code not in (200, 201):
            self._fail("validation",
                       f"Failed to mint the applied improver version: {create_resp.text[:200]}")
        new_improver_id = create_resp.json().get("id", new_id)
        self._transition_change_set(client, change_set_id, "applied")
        if previous:
            from av_cli.core import atomic_write_text

            _improver_dir(self.path).mkdir(parents=True, exist_ok=True)
            atomic_write_text(_improver_dir(self.path) / "last_good", previous)
        _set_current(self.path, new_improver_id)
        return {"change_set_id": change_set_id, "new_improver_id": new_improver_id,
                "previous_improver_id": previous}

    def improver_promote(self, candidate: str | None = None, *, into_branch: str = "main",
                         force: bool = False, dry_run: bool = False) -> dict:
        from av_cli.cmd_improver import (_evaluate_improver_policy, _promoted_path,
                                         current_improver_id, load_improver_policies)
        from av_cli.core import atomic_write_text

        candidate = candidate or current_improver_id(self.path)
        if not candidate:
            self._fail("validation", "No candidate improver: pass one, or improver_use() first.")
        pol = load_improver_policies(self.path).get(into_branch)

        allowed, reason, deciding_rule = True, "no improver policy armed", None
        if pol and not force:
            client = self._online_client()
            allowed, reason, deciding_rule = _evaluate_improver_policy(
                self.path, client, self._cfg(), pol, candidate)
        elif force and pol:
            reason, deciding_rule = f"improver policy BYPASSED via force on '{into_branch}'", "force"

        if dry_run:
            return {"dry_run": True, "decision": "allow" if allowed else "deny",
                    "rule": deciding_rule, "reason": reason}

        self._freeze_guard()
        if not allowed:
            self._fail("review_required" if deciding_rule == "require_review" else "policy_denied",
                       reason)
        atomic_write_text(_promoted_path(self.path, into_branch), candidate)
        return {"allowed": True, "reason": reason, "rule": deciding_rule,
                "into": into_branch, "candidate": candidate}

    def improver_rollback(self, target_id: str | None = None) -> dict:
        from av_cli.cmd_improver import _improver_dir, _set_current

        last_good_path = _improver_dir(self.path) / "last_good"
        target_id = target_id or (last_good_path.read_text(encoding="utf-8").strip()
                                  if last_good_path.exists() else None)
        if not target_id:
            self._fail("validation",
                       "No rollback target: pass target_id, or apply a change set first.")
        _set_current(self.path, target_id)
        return {"active_improver_id": target_id}

    # -- canaries -----------------------------------------------------------------

    def canary_run(self, name: str, *, improver_id: str | None = None) -> dict:
        from av_cli import casobj
        from av_cli.cmd_canary import _load_registry
        from av_cli.cmd_improver import current_improver_id
        from av_cli.cmd_policy import _OPS
        from av_cli.handoff import load_commit, resolve_head

        object_id = _load_registry(self.path).get(name)
        if not object_id:
            self._fail("validation", f"Unknown canary suite: {name} — register it via `av canary register` first.")
        suite = casobj.read_object(self.path, object_id)
        if suite is None:
            self._fail("validation", f"Canary suite object {object_id} is missing locally.")
        _, head_hash = resolve_head(self.path)
        metrics = (load_commit(self.path, head_hash) or {}).get("metrics", {}) if head_hash else {}

        results, all_passed = [], True
        for check in suite["checks"]:
            metric, op, threshold = check.get("metric"), check.get("op", "<"), check.get("threshold")
            value = metrics.get(metric)
            ok = value is not None and op in _OPS and threshold is not None and _OPS[op](value, threshold)
            all_passed = all_passed and ok
            results.append({"name": check.get("name", metric), "metric": metric, "op": op,
                            "threshold": threshold, "value": value, "passed": ok})

        improver_id = improver_id or current_improver_id(self.path)
        client = self._client()
        reported = False
        if improver_id and client.server_available() and client.upload_object(
                casobj.object_path(self.path, object_id), object_id):
            resp = client.session.post(f"{client.server_url}/api/canary-results", json={
                "project_id": self._cfg()["project_id"], "improver_id": improver_id,
                "suite_object_id": object_id, "passed": all_passed, "details": {"checks": results},
            })
            reported = resp.status_code in (200, 201)
        return {"name": name, "passed": all_passed, "checks": results,
                "reported": reported, "improver_id": improver_id}

    # -- freeze ---------------------------------------------------------------

    def freeze_status(self) -> dict:
        from av_cli.cmd_freeze import project_frozen

        frozen, reason = project_frozen(self.path)
        return {"frozen": frozen, "reason": reason}

    def freeze_set(self, frozen: bool, *, reason: str | None = None) -> dict:
        client = self._online_client()
        resp = client.session.post(
            f"{client.server_url}/api/freeze/{self._cfg()['project_id']}",
            json={"frozen": frozen, "reason": reason})
        if resp.status_code == 403:
            self._fail("scope_denied",
                       "Token lacks the 'admin' scope required to "
                       f"{'freeze' if frozen else 'unfreeze'} this project.")
        if resp.status_code != 200:
            self._fail("validation", f"Registry rejected the freeze request: {resp.text[:200]}")
        return resp.json()

    # -- eval registry ---------------------------------------------------------

    def eval_show(self, suite_id: str) -> dict:
        client = self._online_client()
        resp = client.session.get(f"{client.server_url}/api/eval/suites/{suite_id}")
        if resp.status_code != 200:
            self._fail("validation", f"Unknown eval suite: {suite_id}")
        return resp.json()

    def eval_freeze(self, suite_id: str) -> dict:
        client = self._online_client()
        resp = client.session.post(f"{client.server_url}/api/eval/suites/{suite_id}/freeze")
        if resp.status_code == 403:
            self._fail("scope_denied", "Token lacks the 'eval:write' scope required to freeze a suite.")
        if resp.status_code != 200:
            self._fail("validation", f"Could not freeze {suite_id}: {resp.text[:200]}")
        return resp.json()

    def eval_score(self, suite_id: str, *, run_id: str | None = None,
                   score: dict | None = None) -> dict:
        client = self._online_client()
        resp = client.session.post(f"{client.server_url}/api/eval/results", json={
            "project_id": self._cfg()["project_id"], "suite_id": suite_id,
            "run_id": run_id, "score": score or {},
        })
        if resp.status_code == 403:
            self._fail("scope_denied", "Token lacks the 'scorer' scope required to record a score.")
        if resp.status_code not in (200, 201):
            self._fail("validation", f"Registry rejected the score: {resp.text[:200]}")
        return resp.json()

    def eval_reveal(self, result_id: int) -> dict:
        client = self._online_client()
        resp = client.session.post(f"{client.server_url}/api/eval/results/{result_id}/reveal")
        if resp.status_code == 403:
            self._fail("scope_denied", "Token lacks the 'scorer' scope required to reveal a result.")
        if resp.status_code != 200:
            self._fail("validation", f"Could not reveal result {result_id}: {resp.text[:200]}")
        return resp.json()

    # -- budgets ----------------------------------------------------------------

    def budget_set(self, scope_ref: str, *, scope: str = "run",
                   compute_seconds_limit: float | None = None,
                   storage_bytes_limit: int | None = None, step_limit: int | None = None) -> dict:
        if compute_seconds_limit is None and storage_bytes_limit is None and step_limit is None:
            self._fail("validation", "Provide at least one of the three limit kwargs.")
        client = self._online_client()
        resp = client.session.post(f"{client.server_url}/api/budgets", json={
            "project_id": self._cfg()["project_id"], "scope": scope, "scope_ref": scope_ref,
            "compute_seconds_limit": compute_seconds_limit,
            "storage_bytes_limit": storage_bytes_limit, "step_limit": step_limit,
        })
        if resp.status_code not in (200, 201):
            self._fail("validation", f"Registry rejected the budget: {resp.text[:200]}")
        return resp.json()

    def budget_show(self, budget_id: str) -> dict:
        client = self._online_client()
        resp = client.session.get(f"{client.server_url}/api/budgets/{budget_id}")
        if resp.status_code != 200:
            self._fail("validation", f"Unknown budget: {budget_id}")
        return resp.json()

    def budget_consume(self, budget_id: str, *, compute_seconds: float = 0,
                       storage_bytes: int = 0, steps: int = 0) -> dict:
        """Spend is recorded server-side FIRST either way (never lost); raises
        `BudgetExhaustedError` (exit 17) if any dimension is now over its limit, with
        `.message` naming the exceeded dimensions."""
        client = self._online_client()
        resp = client.session.post(f"{client.server_url}/api/budgets/{budget_id}/consume", json={
            "compute_seconds": compute_seconds, "storage_bytes": storage_bytes, "steps": steps,
        })
        if resp.status_code != 200:
            self._fail("validation", f"Could not record spend: {resp.text[:200]}")
        body = resp.json()
        if body["exhausted"]:
            self._fail("budget_exhausted",
                       f"Budget {budget_id} exhausted on: {', '.join(body['exceeded_dims'])} "
                       "(the spend above was still recorded).")
        return body

    # -- plans --------------------------------------------------------------------

    def plan_create(self, doc: dict) -> dict:
        from av_cli import casobj

        client = self._online_client()
        object_id = casobj.write_object(self.path, doc)
        if not client.upload_object(casobj.object_path(self.path, object_id), object_id):
            self._fail("unreachable_queued", "Failed to upload the plan object.")
        resp = client.session.post(f"{client.server_url}/api/plans",
                                   json={"project_id": self._cfg()["project_id"], "object_id": object_id})
        if resp.status_code not in (200, 201):
            self._fail("validation", f"Registry rejected the plan: {resp.text[:200]}")
        return {**resp.json(), "object_id": object_id}

    def plan_attach(self, plan_id: str, run_id: str) -> dict:
        client = self._online_client()
        resp = client.session.post(f"{client.server_url}/api/runs/{run_id}/plan",
                                   json={"plan_id": plan_id})
        if resp.status_code != 200:
            self._fail("validation", f"Could not attach plan: {resp.text[:200]}")
        return resp.json()

    # -- reviewer gate + critiques ------------------------------------------------

    def review_submit(self, target_id: str, decision: str, *, target_type: str = "improver",
                      comment: str | None = None) -> dict:
        client = self._online_client()
        resp = client.session.post(f"{client.server_url}/api/reviews", json={
            "target_type": target_type, "target_id": target_id, "decision": decision,
            "comment": comment,
        })
        if resp.status_code == 403:
            self._fail("scope_denied", "Token lacks the 'review' scope.")
        if resp.status_code == 422 and "own proposer" in resp.text:
            self._fail("validation", "You proposed this — another identity must review it.")
        if resp.status_code not in (200, 201):
            self._fail("validation", f"Registry rejected the review: {resp.text[:200]}")
        return resp.json()

    def critique_add(self, target_id: str, objection: str, *, target_type: str = "improver") -> dict:
        client = self._online_client()
        resp = client.session.post(f"{client.server_url}/api/critiques",
                                   json={"target_type": target_type, "target_id": target_id,
                                         "objection": objection})
        if resp.status_code not in (200, 201):
            self._fail("validation", f"Could not raise critique: {resp.text[:200]}")
        return resp.json()

    def critique_finalize(self, critique_id: str, action: str, *,
                          resolution: str | None = None) -> dict:
        """`action` is `"resolve"` or `"waive"` — waiving means the objection STANDS but
        is deliberately overridden (requires the `review` scope, always audited)."""
        client = self._online_client()
        resp = client.session.post(f"{client.server_url}/api/critiques/{critique_id}/{action}",
                                   json={"resolution": resolution})
        if resp.status_code == 403:
            self._fail("scope_denied", f"Token lacks the 'review' scope required to {action} a critique.")
        if resp.status_code == 409:
            self._fail("validation", f"Critique {critique_id} is already finalized.")
        if resp.status_code != 200:
            self._fail("validation", f"Could not {action} critique: {resp.text[:200]}")
        return resp.json()

    # -- lessons ------------------------------------------------------------------

    def lessons_update(self, doc: dict) -> dict:
        from av_cli import casobj

        client = self._online_client()
        doc = dict(doc)
        doc.setdefault("updated_at", dt.datetime.now(dt.timezone.utc).isoformat())
        object_id = casobj.write_object(self.path, doc)
        if not client.upload_object(casobj.object_path(self.path, object_id), object_id):
            self._fail("unreachable_queued", "Failed to upload the lessons object.")
        resp = client.session.post(f"{client.server_url}/api/lessons",
                                   json={"project_id": self._cfg()["project_id"], "object_id": object_id})
        if resp.status_code not in (200, 201):
            self._fail("validation", f"Registry rejected the lessons update: {resp.text[:200]}")
        return {**resp.json(), "object_id": object_id}

    def lessons_show(self, *, project_id: str | None = None) -> dict | None:
        from av_cli import casobj

        client = self._online_client()
        resp = client.session.get(f"{client.server_url}/api/lessons/latest",
                                  params={"project_id": project_id or self._cfg().get("project_id")})
        if resp.status_code != 200:
            return None
        row = resp.json()
        doc = casobj.read_object(self.path, row["object_id"])
        if doc is None and client.download_object(row["object_id"],
                                                  casobj.object_path(self.path, row["object_id"])):
            doc = casobj.read_object(self.path, row["object_id"])
        return {**row, "document": doc}

    # -- blackboard -----------------------------------------------------------

    def blackboard_post(self, claim: str, *, evidence: list[dict] | None = None) -> dict:
        client = self._online_client()
        resp = client.session.post(f"{client.server_url}/api/blackboard", json={
            "project_id": self._cfg()["project_id"], "claim": claim, "evidence": evidence or [],
        })
        if resp.status_code not in (200, 201):
            self._fail("validation", f"Registry rejected the claim: {resp.text[:200]}")
        return resp.json()

    def blackboard_resolve(self, entry_id: str) -> dict:
        client = self._online_client()
        resp = client.session.post(f"{client.server_url}/api/blackboard/{entry_id}/resolve")
        if resp.status_code != 200:
            self._fail("validation", f"Could not resolve {entry_id}: {resp.text[:200]}")
        return resp.json()

    # -- strategy memory --------------------------------------------------------

    def strategy_add(self, technique: str, outcome: str, *, hyperparameters: dict | None = None,
                     data_mix: dict | None = None, run_ids: list[str] | None = None) -> dict:
        client = self._online_client()
        resp = client.session.post(f"{client.server_url}/api/strategy", json={
            "project_id": self._cfg()["project_id"], "technique": technique, "outcome": outcome,
            "hyperparameters": hyperparameters, "data_mix": data_mix, "run_ids": run_ids or [],
        })
        if resp.status_code not in (200, 201):
            self._fail("validation", f"Registry rejected the strategy entry: {resp.text[:200]}")
        return resp.json()

    # -- causal lineage + search ------------------------------------------------

    def lineage_link(self, cause_type: str, cause_ref: str, effect_metric: str, *,
                     effect_delta: float | None = None, verified: bool = False) -> dict:
        client = self._online_client()
        resp = client.session.post(f"{client.server_url}/api/causal-links", json={
            "project_id": self._cfg()["project_id"], "cause_type": cause_type, "cause_ref": cause_ref,
            "effect_metric": effect_metric, "effect_delta": effect_delta, "verified": verified,
        })
        if resp.status_code not in (200, 201):
            self._fail("validation", f"Registry rejected the causal link: {resp.text[:200]}")
        return resp.json()

    def search_runs(self, metric: str, *, direction: str = "up", min_delta: float = 0.0,
                    project_id: str | None = None) -> list[dict]:
        client = self._online_client()
        resp = client.session.get(f"{client.server_url}/api/search/runs", params={
            "project_id": project_id or self._cfg().get("project_id"), "metric": metric,
            "direction": direction, "min_delta": min_delta,
        })
        if resp.status_code != 200:
            self._fail("validation", f"Search failed: {resp.text[:200]}")
        return resp.json().get("matches", [])

    # -- sandbox + tool manifests -------------------------------------------------

    def sandbox_run(self, command: list[str], *, driver: str = "local", job_id: str | None = None,
                    improver_id: str | None = None, mounts=None, network: str = "none",
                    cpu_limit: float | None = None, memory_limit_mb: int | None = None,
                    gpu: bool = False, timeout_secs: int = 3600) -> dict:
        from av_cli.cmd_sandbox import _report_job
        from av_cli.sandbox.base import JobSpec, get_driver
        from av_cli.sandbox.manifest import load_manifest, verify_spec_against_manifest

        job_id = job_id or str(uuid.uuid4())
        spec = JobSpec(job_id=job_id, command=list(command), cwd=self.path,
                       mounts=list(mounts or []), network=network, cpu_limit=cpu_limit,
                       memory_limit_mb=memory_limit_mb, gpu=gpu, timeout_secs=timeout_secs)
        manifest = load_manifest(self.path, improver_id or "")
        ok, reason = verify_spec_against_manifest(spec, manifest)
        if not ok:
            _report_job(self.path, job_id, driver, improver_id, list(command), "failed")
            self._fail("validation", f"Tool manifest violation: {reason}")
        try:
            status = get_driver(driver, self.path).submit(spec)
        except ValueError as exc:
            self._fail("validation", str(exc))
        _report_job(self.path, job_id, driver, improver_id, list(command), status.state)
        result = {"job_id": job_id, "driver": driver, "state": status.state,
                 "exit_code": status.exit_code, "message": status.message}
        if status.state == "failed":
            # Matches `av sandbox run`'s contract: a failed job is a failure the caller
            # must handle, not a silent success-shaped dict.
            self._fail("validation", f"Sandbox job {job_id} failed: {status.message}")
        return result

    def sandbox_status(self, job_id: str, *, driver: str) -> dict:
        from av_cli.cmd_sandbox import _report_status
        from av_cli.sandbox.base import get_driver

        try:
            status = get_driver(driver, self.path).status(job_id)
        except ValueError as exc:
            self._fail("validation", str(exc))
        _report_status(self.path, job_id, status.state, status.exit_code)
        return {"job_id": job_id, "state": status.state, "exit_code": status.exit_code,
                "message": status.message}

    def tool_manifest_set(self, improver_id: str, *, writable_paths=None, network: str | None = None,
                          network_destinations=None, gpu: bool | None = None,
                          publish: bool = False) -> dict:
        from av_cli.sandbox.manifest import load_manifest, save_manifest

        manifest = dict(load_manifest(self.path, improver_id))
        if writable_paths:
            manifest["writable_paths"] = list(writable_paths)
        if network is not None:
            manifest["network"] = network
        if network_destinations:
            manifest["network_destinations"] = list(network_destinations)
        if gpu is not None:
            manifest["gpu"] = gpu
        save_manifest(self.path, improver_id, manifest)

        published_id = None
        if publish:
            from av_cli import casobj

            client = self._online_client()
            object_id = casobj.write_object(self.path, manifest)
            if not client.upload_object(casobj.object_path(self.path, object_id), object_id):
                self._fail("unreachable_queued", "Failed to upload the manifest object.")
            resp = client.session.post(f"{client.server_url}/api/tool-manifests", json={
                "project_id": self._cfg()["project_id"], "improver_id": improver_id,
                "object_id": object_id,
            })
            if resp.status_code not in (200, 201):
                self._fail("validation", f"Registry rejected the manifest: {resp.text[:200]}")
            published_id = resp.json().get("id")
        return {"improver_id": improver_id, "manifest": manifest, "published_id": published_id}
