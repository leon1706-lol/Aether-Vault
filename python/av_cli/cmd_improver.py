"""av improver — versioned improver artifacts, self-edit proposals, and the improver
promotion gate (v1.3.1, RSI R1: todo.md A.1-A.5, C.11, C.14).

An "improver version" is the agent's OWN stack — code paths, prompt files, tool schemas,
and a policy-pack pointer — content-addressed exactly like everything else in this repo
(`casobj.py`, the same pattern `env_snapshot_id` established). A version's manifest is a
CAS object; the server keeps a lightweight index row (`improver_versions`) over it for
lineage (`parent_id` chains, same shape as `runs.parent_run_id`).

Design note: this file DELIBERATELY does NOT touch `.av/policies.json` or any of
`cmd_policy.py`'s existing load/save/evaluate functions — the improver promotion gate
lives in its own sibling file, `.av/improver_policy.json`, with the same branch-keyed
shape. Folding both into one "policy_version: 2" envelope was considered and rejected:
every existing model-gate reader (`enforce_policy()`, `promote()`, `av policy
set/list/remove`) does `load_policies(repo_root).get(branch)` directly on the top-level
dict — changing that shape would be a breaking change to a contract `tests/test_v120.py`
already pins, for zero functional benefit. Two small files, one per concern, is simpler
and strictly additive.
"""
import datetime
import json
import os

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, resolve_remote


# ---------------------------------------------------------------------------
# Local state: `.av/improver/current` (active pointer) + `.av/improver_policy.json`
# ---------------------------------------------------------------------------

def _improver_dir(repo_root):
    return repo_root / ".av" / "improver"


def _current_path(repo_root):
    return _improver_dir(repo_root) / "current"


def current_improver_id(repo_root) -> str | None:
    """The locally active improver version id, or None if none is set yet."""
    p = _current_path(repo_root)
    if not p.exists():
        return None
    val = p.read_text(encoding="utf-8").strip()
    return val or None


def _set_current(repo_root, improver_id: str) -> None:
    _improver_dir(repo_root).mkdir(parents=True, exist_ok=True)
    atomic_write_text(_current_path(repo_root), improver_id)


def _improver_policy_path(repo_root):
    return repo_root / ".av" / "improver_policy.json"


def load_improver_policies(repo_root) -> dict:
    """{"<branch>": {"require_canaries": bool, "require_signature": bool}} — the improver-
    gate sibling of `cmd_policy.py::load_policies()`, same on-disk conventions (atomic
    write, empty-on-any-error read), deliberately its own file (see module docstring)."""
    path = _improver_policy_path(repo_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_improver_policies(repo_root, policies: dict) -> None:
    atomic_write_text(_improver_policy_path(repo_root),
                      json.dumps(policies, indent=2, sort_keys=True))


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


def _require_online(repo_root):
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued",
             f"Registry unreachable at {client.server_url} — improver versioning needs "
             "the registry (no offline queue for this artifact type yet).")
    return client


def _hash_paths(repo_root, raw_paths: tuple) -> list[dict]:
    out = []
    for raw in raw_paths:
        fpath = Path(raw)
        if not fpath.is_absolute():
            fpath = repo_root / fpath
        if not fpath.exists() or not fpath.is_file():
            continue
        rel = str(fpath.resolve().relative_to(repo_root.resolve())) if _is_within(fpath, repo_root) else str(fpath)
        out.append({"path": rel.replace(os.sep, "/"), "hash": hash_file_safe(str(fpath))})
    return out


def _is_within(path, root) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


@click.group()
def improver() -> None:
    """Versioned improver artifacts (agent code/prompts/tools/policy) + self-edit lifecycle."""


@improver.command("register")
@click.option("--code", "code_paths", multiple=True, help="Agent code file to include (repeatable).")
@click.option("--prompt", "prompt_paths", multiple=True, help="Prompt file to include (repeatable).")
@click.option("--tool-schema", "tool_schema_paths", multiple=True,
              help="Tool-schema file to include (repeatable).")
@click.option("--parent", "parent_id", default=None,
              help="Parent improver version id (defaults to the current pointer, if any).")
@click.option("--policy-pack", "policy_pack_id", default=None,
              help="Policy pack id this improver version pins (see `av policy pack`).")
@click.option("--sign/--no-sign", default=True, show_default=True,
              help="Sign the manifest with this repo's ed25519 key if one exists "
                   "(`av registry keygen`) — never fails registration if no key is present.")
def improver_register(code_paths, prompt_paths, tool_schema_paths, parent_id, policy_pack_id, sign):
    """Register a new improver version from the given code/prompt/tool-schema files."""
    from . import casobj
    from .cmd_freeze import freeze_guard

    repo_root = ensure_repo()
    freeze_guard(repo_root)
    client = _require_online(repo_root)
    parent_id = parent_id or current_improver_id(repo_root)

    manifest = {
        "kind": "improver_manifest",
        "manifest_version": "1.0",
        "parent_id": parent_id,
        "code": _hash_paths(repo_root, code_paths),
        "prompts": _hash_paths(repo_root, prompt_paths),
        "tool_schemas": _hash_paths(repo_root, tool_schema_paths),
        "policy_pack_id": policy_pack_id,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if sign:
        sig = casobj.sign_object(manifest, repo_root)
        if sig:
            manifest["signature"] = sig

    manifest_id = casobj.write_object(repo_root, manifest)
    obj_path = casobj.object_path(repo_root, manifest_id)
    if not client.upload_object(obj_path, manifest_id):
        fail(None, "unreachable_queued", "Failed to upload the improver manifest object.")

    cfg = load_config(repo_root)
    import uuid as _uuid

    new_id = str(_uuid.uuid4())
    resp = client.session.post(f"{client.server_url}/api/improvers", json={
        "id": new_id, "project_id": cfg["project_id"],
        "manifest_object_id": manifest_id, "parent_id": parent_id,
    })
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Registry rejected the improver version: {resp.text[:200]}")
    body = resp.json()
    improver_id = body.get("id", new_id)
    _set_current(repo_root, improver_id)

    if current_output_mode() == "json":
        emit_json(None, "improver register", data={
            "id": improver_id, "manifest_object_id": manifest_id, "parent_id": parent_id,
            "code_files": len(manifest["code"]), "prompt_files": len(manifest["prompts"]),
            "tool_schema_files": len(manifest["tool_schemas"]),
            "signed": "signature" in manifest,
        })
        return
    click.secho(f"Improver version {improver_id} registered "
               f"({len(manifest['code'])} code, {len(manifest['prompts'])} prompt, "
               f"{len(manifest['tool_schemas'])} tool-schema files)"
               + (", signed" if "signature" in manifest else ""), fg="green")


@improver.command("init")
@click.pass_context
def improver_init(ctx):
    """Register the FIRST improver version (no parent, no files) — a baseline root to
    build lineage from. Shorthand for `av improver register` with nothing else set."""
    ctx.invoke(improver_register, code_paths=(), prompt_paths=(), tool_schema_paths=(),
              parent_id=None, policy_pack_id=None, sign=True)


@improver.command("current")
def improver_current():
    """Show the locally active improver version id, if any."""
    repo_root = ensure_repo()
    cur = current_improver_id(repo_root)
    if current_output_mode() == "json":
        emit_json(None, "improver current", data={"id": cur})
        return
    click.echo(cur or "(none — run `av improver init` or `av improver use ID`)")


@improver.command("use")
@click.argument("improver_id")
def improver_use(improver_id: str):
    """Set the locally active improver version pointer (does not validate against the
    registry — use `av improver show ID` first if you want to confirm it exists)."""
    repo_root = ensure_repo()
    _set_current(repo_root, improver_id)
    if current_output_mode() == "json":
        emit_json(None, "improver use", data={"id": improver_id})
        return
    click.secho(f"Active improver version set to {improver_id}", fg="green")


@improver.command("list")
@click.option("--project", "project_id", default=None, help="Defaults to this repo's project.")
@click.option("--limit", default=20, show_default=True)
def improver_list(project_id: str | None, limit: int):
    """List improver versions known to the registry."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/improvers",
                              params={"project_id": project_id or cfg.get("project_id"),
                                      "limit": limit})
    rows = resp.json().get("improvers", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "improver list", data={"improvers": rows})
        return
    if not rows:
        click.secho("No improver versions registered yet.", fg="yellow")
        return
    cur = current_improver_id(repo_root)
    for r in rows:
        marker = "*" if r["id"] == cur else " "
        click.echo(f" {marker}{r['id'][:8]}  parent={((r.get('parent_id') or '-')[:8])}  "
                   f"{r.get('created_at', '')}")


@improver.command("show")
@click.argument("improver_id")
def improver_show(improver_id: str):
    """Show one improver version's manifest content."""
    from . import casobj

    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/improvers/{improver_id}")
    if resp.status_code != 200:
        fail(None, "validation", f"Unknown improver version: {improver_id}")
    row = resp.json()
    manifest = casobj.read_object(repo_root, row["manifest_object_id"])
    if manifest is None:
        client.download_object(row["manifest_object_id"],
                               casobj.object_path(repo_root, row["manifest_object_id"]))
        manifest = casobj.read_object(repo_root, row["manifest_object_id"])
    if current_output_mode() == "json":
        emit_json(None, "improver show", data={**row, "manifest": manifest})
        return
    click.secho(f"Improver {row['id']}", bold=True)
    click.echo(f"  parent: {row.get('parent_id') or '-'}")
    click.echo(f"  created_by: {row.get('created_by') or '-'} at {row.get('created_at')}")
    if manifest:
        click.echo(f"  code files: {len(manifest.get('code', []))}")
        click.echo(f"  prompt files: {len(manifest.get('prompts', []))}")
        click.echo(f"  tool-schema files: {len(manifest.get('tool_schemas', []))}")
        click.echo(f"  signed: {'signature' in manifest}")


@improver.command("lineage")
@click.argument("improver_id")
@click.option("--depth", default=50, show_default=True)
def improver_lineage(improver_id: str, depth: int):
    """Walk one improver version's parent chain."""
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/improvers/{improver_id}/lineage",
                              params={"depth": depth})
    if resp.status_code != 200:
        fail(None, "validation", f"Unknown improver version: {improver_id}")
    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "improver lineage", data=body)
        return
    for node in body.get("lineage", []):
        click.echo(f"  {node['id'][:8]}  {node.get('created_at', '')}")


# ---------------------------------------------------------------------------
# Self-edit proposals (change sets) — todo.md A.3/A.4
# ---------------------------------------------------------------------------

_RISK_LEVELS = ("low", "medium", "high")


@improver.command("propose")
@click.option("--diff", "diff_file", required=True, type=click.Path(exists=True, dir_okay=False),
              help="The proposed change, as a unified diff (or any text — stored verbatim).")
@click.option("--rationale", required=True, help="Why this change is being proposed.")
@click.option("--risk", type=click.Choice(_RISK_LEVELS), default="low", show_default=True,
              help="Predicted risk of applying this change.")
@click.option("--improver", "improver_id", default=None,
              help="Which improver version this proposes to change (defaults to current).")
def improver_propose(diff_file: str, rationale: str, risk: str, improver_id: str | None):
    """Submit a structured self-edit proposal: diff + rationale + predicted risk."""
    from . import casobj
    from .cmd_freeze import freeze_guard

    repo_root = ensure_repo()
    freeze_guard(repo_root)
    client = _require_online(repo_root)
    improver_id = improver_id or current_improver_id(repo_root)

    diff_text = Path(diff_file).read_text(encoding="utf-8")
    doc = {
        "kind": "change_set", "improver_id": improver_id, "diff": diff_text,
        "rationale": rationale, "risk": risk,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    sig = casobj.sign_object(doc, repo_root)
    if sig:
        doc["signature"] = sig
    object_id = casobj.write_object(repo_root, doc)
    if not client.upload_object(casobj.object_path(repo_root, object_id), object_id):
        fail(None, "unreachable_queued", "Failed to upload the change-set object.")

    cfg = load_config(repo_root)
    import uuid as _uuid

    cs_id = str(_uuid.uuid4())
    resp = client.session.post(f"{client.server_url}/api/change-sets", json={
        "id": cs_id, "project_id": cfg["project_id"], "improver_id": improver_id,
        "object_id": object_id, "risk": risk,
    })
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Registry rejected the proposal: {resp.text[:200]}")
    body = resp.json()
    cs_id = body.get("id", cs_id)

    if current_output_mode() == "json":
        emit_json(None, "improver propose", data={"id": cs_id, "improver_id": improver_id,
                                                   "risk": risk, "object_id": object_id})
        return
    click.secho(f"Change set {cs_id} proposed against improver "
               f"{(improver_id or '(none)')[:8]} (risk: {risk}).", fg="green")


def _transition(repo_root, cs_id: str, new_status: str) -> dict:
    client = _require_online(repo_root)
    resp = client.session.post(f"{client.server_url}/api/change-sets/{cs_id}/status",
                               json={"status": new_status})
    if resp.status_code != 200:
        fail(None, "validation",
             f"Cannot transition change set {cs_id} to {new_status!r}: {resp.text[:200]}")
    return resp.json()


@improver.command("review")
@click.argument("change_set_id")
@click.option("--approve", "decision", flag_value="approved", help="Approve the proposal.")
@click.option("--reject", "decision", flag_value="rejected", help="Reject the proposal.")
def improver_review(change_set_id: str, decision: str | None):
    """Approve or reject a proposed change set (required before `av improver apply`)."""
    if not decision:
        fail(None, "validation", "Pass exactly one of --approve or --reject.")
    repo_root = ensure_repo()
    result = _transition(repo_root, change_set_id, decision)
    if current_output_mode() == "json":
        emit_json(None, "improver review", data=result)
        return
    click.secho(f"Change set {change_set_id} -> {decision}", fg="green" if decision == "approved" else "yellow")


@improver.command("apply")
@click.argument("change_set_id")
def improver_apply(change_set_id: str):
    """Apply an APPROVED change set: mints the next improver version (parented on the
    change set's improver_id) and marks the change set 'applied'.

    Scope note (v1.3.1 R1): this records the version transition and its audit trail —
    the mechanical diff execution inside an isolated sandbox is `av sandbox run` (R5,
    todo.md G.29), which a future improver-registration step can invoke before calling
    this to make the result official. Applying without executing anything is intentional
    for a proposal whose change is metadata/config/policy-only.
    """
    from .cmd_freeze import freeze_guard

    repo_root = ensure_repo()
    freeze_guard(repo_root)
    client = _require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/change-sets/{change_set_id}")
    if resp.status_code != 200:
        fail(None, "validation", f"Unknown change set: {change_set_id}")
    cs = resp.json()
    if cs["status"] != "approved":
        fail(None, "validation",
             f"Change set {change_set_id} is '{cs['status']}', not 'approved' — "
             f"run `av improver review {change_set_id} --approve` first.")

    previous = current_improver_id(repo_root)
    cfg = load_config(repo_root)
    import uuid as _uuid

    new_id = str(_uuid.uuid4())
    create_resp = client.session.post(f"{client.server_url}/api/improvers", json={
        "id": new_id, "project_id": cfg["project_id"],
        "manifest_object_id": _parent_manifest_object_id(client, cs.get("improver_id"))
                              or _empty_manifest_object_id(repo_root, client),
        "parent_id": cs.get("improver_id"),
    })
    if create_resp.status_code not in (200, 201):
        fail(None, "validation", f"Failed to mint the applied improver version: {create_resp.text[:200]}")
    new_improver_id = create_resp.json().get("id", new_id)

    _transition(repo_root, change_set_id, "applied")
    if previous:
        _improver_dir(repo_root).mkdir(parents=True, exist_ok=True)
        atomic_write_text(_improver_dir(repo_root) / "last_good", previous)
    _set_current(repo_root, new_improver_id)

    if current_output_mode() == "json":
        emit_json(None, "improver apply", data={"change_set_id": change_set_id,
                                                 "new_improver_id": new_improver_id,
                                                 "previous_improver_id": previous})
        return
    click.secho(f"Change set {change_set_id} applied -> improver {new_improver_id}", fg="green")


def _parent_manifest_object_id(client, improver_id: str | None) -> str | None:
    if not improver_id:
        return None
    resp = client.session.get(f"{client.server_url}/api/improvers/{improver_id}")
    return resp.json().get("manifest_object_id") if resp.status_code == 200 else None


def _empty_manifest_object_id(repo_root, client) -> str:
    """A change-set applied against no known parent still needs SOME manifest object to
    point at — an explicitly-empty one, distinguishable from a real registered manifest
    by its `code`/`prompts`/`tool_schemas` all being empty lists."""
    from . import casobj

    doc = {"kind": "improver_manifest", "manifest_version": "1.0", "parent_id": None,
          "code": [], "prompts": [], "tool_schemas": [], "policy_pack_id": None,
          "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    oid = casobj.write_object(repo_root, doc)
    client.upload_object(casobj.object_path(repo_root, oid), oid)
    return oid


@improver.group("policy")
def improver_policy_group() -> None:
    """Improver-gate rules for `av improver promote` (sibling of `av policy`, which
    gates the MODEL — see module docstring for why these are separate files)."""


@improver_policy_group.command("set")
@click.argument("branch")
@click.option("--require-canaries/--no-require-canaries", default=None,
              help="Deny promotion without a passing canary result for the candidate.")
@click.option("--require-signature/--no-require-signature", default=None,
              help="Deny promotion of an unsigned improver manifest.")
@click.option("--require-review/--no-require-review", default=None,
              help="Deny promotion without an approving review on file for the "
                   "candidate (todo.md H.34) — exits 19, not 16, when this is the "
                   "deciding rule. Also blocks on any unresolved/un-waived critique.")
def improver_policy_set(branch: str, require_canaries: bool | None, require_signature: bool | None,
                        require_review: bool | None) -> None:
    """Arm (or adjust) the improver-promotion gate for BRANCH."""
    repo_root = ensure_repo()
    pol = load_improver_policies(repo_root)
    entry = dict(pol.get(branch, {}))
    if require_canaries is not None:
        entry["require_canaries"] = require_canaries
    if require_signature is not None:
        entry["require_signature"] = require_signature
    if require_review is not None:
        entry["require_review"] = require_review
    if not entry:
        fail(None, "validation",
             "Provide --require-canaries and/or --require-signature and/or --require-review.")
    pol[branch] = entry
    save_improver_policies(repo_root, pol)
    if current_output_mode() == "json":
        emit_json(None, "improver policy set", data={"branch": branch, "policy": entry})
        return
    click.secho(f"Improver policy armed for '{branch}': {entry}", fg="green")


@improver_policy_group.command("list")
def improver_policy_list() -> None:
    repo_root = ensure_repo()
    pol = load_improver_policies(repo_root)
    if current_output_mode() == "json":
        emit_json(None, "improver policy list", data={"policies": pol})
        return
    if not pol:
        click.secho("No improver policies armed.", fg="yellow")
        return
    for br, p in sorted(pol.items()):
        click.echo(f"  {br}: {json.dumps(p)}")


@improver_policy_group.command("remove")
@click.argument("branch")
def improver_policy_remove(branch: str) -> None:
    repo_root = ensure_repo()
    pol = load_improver_policies(repo_root)
    if branch in pol:
        del pol[branch]
        save_improver_policies(repo_root, pol)
        click.secho(f"Improver policy removed for '{branch}'.", fg="green")
    else:
        click.secho(f"No improver policy for '{branch}'.", fg="yellow")


def _promoted_path(repo_root, branch: str):
    d = _improver_dir(repo_root) / "promoted"
    d.mkdir(parents=True, exist_ok=True)
    return d / branch


def _evaluate_improver_policy(repo_root, client, cfg, pol, candidate) -> tuple[bool, str, str | None]:
    """Evaluates each armed rule in order; returns (allowed, reason, deciding_rule) on
    the FIRST denial, or (True, "improver policy satisfied", None) if every armed rule
    passes. Kept as one function (rather than the R1 if/elif chain) now that there are
    three independent gates — a fourth is a one-branch addition here, not a rewrite."""
    from . import casobj
    from .cmd_canary import latest_canary_passed

    if pol.get("require_canaries"):
        if not latest_canary_passed(repo_root, client, cfg["project_id"], candidate):
            return False, "require_canaries: no passing canary result found", "require_canaries"

    if pol.get("require_signature"):
        resp = client.session.get(f"{client.server_url}/api/improvers/{candidate}")
        manifest = None
        if resp.status_code == 200:
            manifest_id = resp.json().get("manifest_object_id")
            manifest = casobj.read_object(repo_root, manifest_id)
            if manifest is None and client.download_object(
                    manifest_id, casobj.object_path(repo_root, manifest_id)):
                manifest = casobj.read_object(repo_root, manifest_id)
        sig_ok, sig_reason = casobj.verify_object(manifest or {})
        if not sig_ok:
            return False, f"require_signature: {sig_reason}", "require_signature"

    if pol.get("require_review"):
        resp = client.session.get(f"{client.server_url}/api/reviews",
                                  params={"target_type": "improver", "target_id": candidate})
        reviews = resp.json().get("reviews", []) if resp.status_code == 200 else []
        if not any(r["decision"] == "approve" for r in reviews):
            return False, "require_review: no approving review on file for this improver version", "require_review"
        crit_resp = client.session.get(f"{client.server_url}/api/critiques",
                                       params={"target_type": "improver", "target_id": candidate,
                                               "status": "open"})
        open_critiques = crit_resp.json().get("critiques", []) if crit_resp.status_code == 200 else []
        if open_critiques:
            return False, (f"require_review: {len(open_critiques)} open critique(s) must be "
                          "resolved or waived first"), "require_review"

    return True, "improver policy satisfied", None


@improver.command("promote")
@click.argument("candidate", required=False, default=None)
@click.option("--into", "into_branch", default="main", show_default=True)
@click.option("--force", is_flag=True, default=False, help="Bypass the armed improver policy explicitly.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Evaluate the decision without landing anything — exits 0 for both "
                   "decisions (a script branches on data.decision, not the exit code), "
                   "same contract as `av promote --dry-run`.")
def improver_promote(candidate: str | None, into_branch: str, force: bool, dry_run: bool) -> None:
    """Dual-gate promotion for the IMPROVER (not the model — see plain `av promote`):
    evaluates `.av/improver_policy.json`'s rules for INTO_BRANCH (require_canaries,
    require_signature, require_review) against CANDIDATE (defaults to the current pointer)."""
    repo_root = ensure_repo()
    candidate = candidate or current_improver_id(repo_root)
    if not candidate:
        fail(None, "validation", "No candidate improver: pass one, or `av improver use ID` first.")
    pol = load_improver_policies(repo_root).get(into_branch)

    allowed, reason, deciding_rule = True, "no improver policy armed", None
    if pol and not force:
        client = _require_online(repo_root)
        cfg = load_config(repo_root)
        allowed, reason, deciding_rule = _evaluate_improver_policy(repo_root, client, cfg, pol, candidate)
    elif force and pol:
        reason, deciding_rule = f"improver policy BYPASSED via --force on '{into_branch}'", "force"

    if dry_run:
        decision = "allow" if allowed else "deny"
        if current_output_mode() == "json":
            emit_json(None, "improver promote", data={"dry_run": True, "decision": decision,
                                                       "rule": deciding_rule, "reason": reason})
            return
        click.secho(f"[DRY RUN] {decision.upper()}: {reason}", fg="green" if allowed else "red")
        return

    from .cmd_freeze import freeze_guard

    freeze_guard(repo_root)

    if not allowed:
        if current_output_mode() == "json":
            emit_json(None, "improver promote", data={"allowed": False, "reason": reason,
                                                       "rule": deciding_rule})
        else:
            click.secho(f"DENIED: {reason}", fg="red", err=True)
        # require_review denies with 19 (review_required) — a distinct signal from every
        # other denial (16, policy_denied): "nobody has signed off yet" is a different
        # remediation ("get it reviewed") than "the metrics/signature don't qualify".
        raise SystemExit(EXIT_REVIEW_REQUIRED if deciding_rule == "require_review" else EXIT_POLICY_DENIED)

    atomic_write_text(_promoted_path(repo_root, into_branch), candidate)
    if current_output_mode() == "json":
        emit_json(None, "improver promote", data={"allowed": True, "reason": reason,
                                                   "rule": deciding_rule, "into": into_branch,
                                                   "candidate": candidate})
        return
    click.secho(f"Improver {candidate} promoted into '{into_branch}': {reason}", fg="green")


@improver.command("rollback")
@click.option("--to", "target_id", default=None,
              help="Roll back to this improver version id (defaults to `.av/improver/last_good`).")
def improver_rollback(target_id: str | None):
    """One-command rollback to the last known-good improver version."""
    repo_root = ensure_repo()
    last_good_path = _improver_dir(repo_root) / "last_good"
    target_id = target_id or (last_good_path.read_text(encoding="utf-8").strip()
                              if last_good_path.exists() else None)
    if not target_id:
        fail(None, "validation",
             "No rollback target: pass --to ID, or apply a change set first "
             "(that's what records `.av/improver/last_good`).")
    _set_current(repo_root, target_id)
    if current_output_mode() == "json":
        emit_json(None, "improver rollback", data={"active_improver_id": target_id})
        return
    click.secho(f"Rolled back — active improver version is now {target_id}", fg="green")
