"""av branch protect / av promote — promotion guardrails for autonomous loops (v1.2.0).
Policies live in .av/policies.json: {"<branch>": {"metric", "op", "baseline_ref",
"require_signature"}} — `metric`/`op` and `require_signature` are independent gates,
either or both may be present. Enforcement is CLIENT-SIDE at merge/push-to-protected-
branch time; signature checking is tamper evidence, not a PKI (no key-to-identity binding).
"""

import contextlib
import io
import json

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import EXIT_POLICY_DENIED, current_output_mode, emit_json


def _policies_path(repo_root):
    return repo_root / ".av" / "policies.json"


def load_policies(repo_root) -> dict:
    path = _policies_path(repo_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_policies(repo_root, policies: dict) -> None:
    atomic_write_text(_policies_path(repo_root), json.dumps(policies, indent=2, sort_keys=True))


def _resolve_ref_ancestor(repo_root, ref_name: str | None) -> str | None:
    """Resolves REF or REF~N (e.g. `main~1`) to a commit hash. `~N` walks N first-parent
    hops back from REF's tip via `handoff._commit_parent()`."""
    from .handoff import _commit_parent, load_commit, resolve_head

    base_name, sep, hops_raw = (ref_name or "").partition("~")
    hops = 0
    if sep:
        try:
            hops = int(hops_raw)
        except ValueError:
            base_name = ref_name  # not a real ~N suffix — fall through, treat literally
            hops = 0

    if base_name in (None, "", "HEAD"):
        cur = resolve_head(repo_root)[1]
    else:
        p = repo_root / ".av" / "refs" / "heads" / base_name
        cur = p.read_text().strip() if p.exists() else base_name  # else: raw commit hash

    seen = set()
    for _ in range(hops):
        if not cur or cur in seen:
            return None
        seen.add(cur)
        cur = _commit_parent(load_commit(repo_root, cur))
    return cur


def _latest_metrics_for_ref(repo_root, ref_name: str | None) -> dict | None:
    """Walks from ref tip backwards, returning the first commit with non-empty metrics.
    `ref_name` may be a branch name, `branch~N` ancestry, or a raw commit hash. Walks via
    `handoff._commit_parent()`, which tolerates both the local `parents`-list shape and
    the registry's `parent_hash` shape."""
    from .handoff import _commit_parent, load_commit

    cur = _resolve_ref_ancestor(repo_root, ref_name)
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        c = load_commit(repo_root, cur)
        if not c:
            break
        m = c.get("metrics") or {}
        if isinstance(m, dict) and m:
            return m
        cur = _commit_parent(c)
    return None


_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


def evaluate(policy: dict, candidate_metrics: dict | None, baseline_metrics: dict | None) -> tuple[bool, str]:
    metric = policy.get("metric")
    op = policy.get("op", "<")
    base_ref = policy.get("baseline_ref")
    if not metric:
        return False, "policy has no metric"
    if op not in _OPS:
        return False, f"unknown operator: {op}"
    cand = (candidate_metrics or {}).get(metric)
    if cand is None:
        return False, f"candidate has no metric '{metric}'"
    if base_ref:
        base = (baseline_metrics or {}).get(metric)
        if base is None:
            return False, f"baseline ref '{base_ref}' has no metric '{metric}'"
        ok = _OPS[op](cand, base)
        return ok, f"{metric}: {cand} {op} {base} (baseline {base_ref}) → {'PASS' if ok else 'DENY'}"
    threshold = policy.get("threshold")
    if threshold is None:
        return False, "policy needs baseline_ref or threshold"
    ok = _OPS[op](cand, float(threshold))
    return ok, f"{metric}: {cand} {op} {threshold} → {'PASS' if ok else 'DENY'}"


@click.group()
def policy() -> None:
    """Branch protection policies evaluated client-side before merges/promotions."""


@policy.command("set")
@click.argument("branch")
@click.argument("metric", required=False, default=None)
@click.argument("op", type=click.Choice(["<", "<=", ">", ">="]), required=False, default=None)
@click.option("--baseline-ref", default=None, help="Compare against this ref's latest metrics.")
@click.option("--threshold", type=float, default=None, help="Absolute threshold instead of a baseline.")
@click.option("--require-signature", "require_signature", is_flag=True, default=False,
              help="v1.2.5: deny promotion/merge of a candidate with no valid embedded "
                   "signature on this branch. Tamper evidence, not a PKI — this does not "
                   "bind a key to an identity. Combine with METRIC/OP for both gates, or "
                   "pass alone (with no METRIC/OP) for a signature-only policy.")
def policy_set(branch: str, metric: str | None, op: str | None, baseline_ref: str | None,
               threshold: float | None, require_signature: bool):
    """Require METRIC OP baseline/threshold and/or a valid signature before writes land on BRANCH.

    METRIC and OP are optional when --require-signature is used alone (a signature-only
    policy, e.g. `av policy set main --require-signature`)."""
    repo_root = ensure_repo()
    pol = load_policies(repo_root)

    if (metric is None) != (op is None):
        fail(None, "validation", "METRIC and OP must be given together (or both omitted for a signature-only policy).")

    entry: dict = {}
    if metric is not None:
        entry["metric"] = metric
        entry["op"] = op
        if baseline_ref:
            entry["baseline_ref"] = baseline_ref
        if threshold is not None:
            entry["threshold"] = threshold
        if "baseline_ref" not in entry and "threshold" not in entry:
            fail(None, "validation", "Provide --baseline-ref or --threshold.")
    elif baseline_ref or threshold is not None:
        fail(None, "validation", "--baseline-ref/--threshold require METRIC and OP.")

    if require_signature:
        entry["require_signature"] = True
    if not entry:
        fail(None, "validation", "Provide METRIC OP (with --baseline-ref/--threshold) and/or --require-signature.")

    pol[branch] = entry
    save_policies(repo_root, pol)
    if current_output_mode() == "json":
        emit_json(None, "policy set", data={"branch": branch, "policy": entry})
        return
    click.secho(f"Policy armed for '{branch}': {entry}", fg="green")


@policy.command("list")
def policy_list():
    repo_root = ensure_repo()
    pol = load_policies(repo_root)
    if current_output_mode() == "json":
        emit_json(None, "policy list", data={"policies": pol})
        return
    if not pol:
        click.secho("No policies armed.", fg="yellow")
        return
    for br, p in sorted(pol.items()):
        click.echo(f"  {br}: {json.dumps(p)}")


@policy.command("remove")
@click.argument("branch")
def policy_remove(branch: str):
    repo_root = ensure_repo()
    pol = load_policies(repo_root)
    if branch in pol:
        del pol[branch]
        save_policies(repo_root, pol)
        click.secho(f"Policy removed for '{branch}'.", fg="green")
    else:
        click.secho(f"No policy for '{branch}'.", fg="yellow")


def candidate_is_signed(repo_root, candidate_ref) -> tuple[bool, str]:
    """For `require_signature` policies — verifies the candidate's own embedded signature
    (not a detached one). Returns (signed_and_valid, reason) so a denial can say why."""
    from .handoff import load_commit
    from .signing import SigningUnavailable, verify_signature

    commit = load_commit(repo_root, candidate_ref) if candidate_ref else None
    if not commit:
        return False, "candidate commit not found"
    if not isinstance(commit.get("signature"), dict):
        return False, "candidate commit is unsigned"
    try:
        ok, reason = verify_signature(commit)
    except SigningUnavailable as exc:
        return False, str(exc)
    return ok, reason


def _report_policy_outcome(repo_root, decision: str, rule: str | None) -> None:
    """Best-effort telemetry pointer from the active run to the policy decision just made
    — never raises, and a silent no-op with no active run or no reachable server."""
    try:
        from .client import VaultClient
        from .core import resolve_remote

        run_id = resolve_run_id(repo_root)
        if not run_id:
            return
        client = VaultClient(*resolve_remote(repo_root))
        client.report_run_policy_outcome(run_id, decision, rule)
    except Exception:
        pass  # telemetry only — a reporting failure must never affect the promotion itself


def enforce_policy(repo_root, target_branch: str, candidate_metrics: dict | None,
                   baseline_metrics_fn, candidate_ref=None) -> None:
    """Raises PolicyDenied (via fail) when TARGET_BRANCH is protected and policy fails.
    `candidate_ref` (optional — the commit being merged/promoted) enables the
    `require_signature` policy field."""
    pol = load_policies(repo_root).get(target_branch)
    if not pol:
        return
    if pol.get("require_signature"):
        signed, sig_reason = candidate_is_signed(repo_root, candidate_ref)
        if not signed:
            _report_policy_outcome(repo_root, "deny", "require_signature")
            fail(None, "policy_denied",
                 f"promotion to '{target_branch}' denied: require_signature is armed and "
                 f"the candidate is not validly signed ({sig_reason}). "
                 "Sign it (`av registry keygen` then re-commit) or override consciously "
                 "with --force.")
    # require_signature is usable standalone — a policy with no "metric" key is a
    # signature-only gate, not an incomplete metric policy.
    if not pol.get("metric"):
        _report_policy_outcome(repo_root, "allow",
                               "require_signature" if pol.get("require_signature") else None)
        return
    base_ref = pol.get("baseline_ref")
    baseline = baseline_metrics_fn(base_ref) if base_ref else None
    ok, reason = evaluate(pol, candidate_metrics, baseline)
    _report_policy_outcome(repo_root, "allow" if ok else "deny", f"metric:{pol['metric']}{pol.get('op', '')}")
    if not ok:
        fail(None, "policy_denied",
             f"promotion to '{target_branch}' denied: {reason}. "
             "Override consciously with --force.")


@click.command()
@click.argument("candidate", required=False, default=None)
@click.option("--into", "into_branch", default="main", show_default=True)
@click.option("--force", is_flag=True, default=False, help="Bypass the armed policy explicitly.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Evaluate the policy decision and which rule would decide it, "
                   "without landing anything — touches nothing either way.")
@click.pass_context
def promote(ctx, candidate: str | None, into_branch: str, force: bool, dry_run: bool) -> None:
    """Evaluate INTO_BRANCH's armed policy against CANDIDATE's latest metrics, then land it."""
    repo_root = ensure_repo()
    pol_entry = load_policies(repo_root).get(into_branch)

    from .handoff import load_commit, resolve_head

    cand_ref = candidate or resolve_head(repo_root)[1]
    cand_commit = load_commit(repo_root, cand_ref) if cand_ref else None
    if cand_commit is None:
        fail(None, "validation", f"Unknown candidate: {candidate}")
    cand_metrics = cand_commit.get("metrics")

    allowed, reason, deciding_rule = True, "no policy armed", None
    if pol_entry and not force:
        # require_signature is checked FIRST -- a denial here should say "unsigned", not a
        # misleading metric-comparison message when the metrics happen to also fail.
        if pol_entry.get("require_signature"):
            signed, sig_reason = candidate_is_signed(repo_root, cand_ref)
            if not signed:
                allowed, reason, deciding_rule = False, (
                    f"require_signature is armed and the candidate is not validly signed "
                    f"({sig_reason})"
                ), "require_signature"
        # require_signature is usable standalone -- evaluate() runs only when a metric IS
        # configured.
        if allowed and pol_entry.get("metric"):
            base_ref = pol_entry.get("baseline_ref")
            baseline = _latest_metrics_for_ref(repo_root, base_ref) if base_ref else None
            allowed, reason = evaluate(pol_entry, cand_metrics, baseline)
            deciding_rule = f"metric:{pol_entry['metric']}{pol_entry.get('op', '')}"
        elif allowed and pol_entry.get("require_signature"):
            reason = "require_signature: candidate is validly signed"
            deciding_rule = "require_signature"
    elif force and pol_entry:
        reason = f"policy BYPASSED via --force on '{into_branch}'"
        deciding_rule = "force"

    if dry_run:
        # Exits 0 for BOTH decisions (a script branches on data.decision, not the exit
        # code) -- dry-run never fails just because the real promotion would have.
        decision = "allow" if allowed else "deny"
        if current_output_mode() == "json":
            emit_json(None, "promote", data={"dry_run": True, "decision": decision,
                                             "rule": deciding_rule, "reason": reason})
            return
        color = "green" if allowed else "red"
        click.secho(f"[DRY RUN] {decision.upper()}: {reason}"
                    + (f" (rule: {deciding_rule})" if deciding_rule else ""), fg=color)
        return

    # Freeze blocks the real landing (never dry-run) even when --force would otherwise
    # bypass the policy itself -- freeze is a higher-priority safety gate than any policy.
    from .cmd_freeze import freeze_guard

    freeze_guard(repo_root)

    # Report the real decision for the active run -- dry runs above are excluded.
    if pol_entry:
        _report_policy_outcome(repo_root, "allow" if allowed else "deny", deciding_rule)

    if not allowed:
        if current_output_mode() == "json":
            emit_json(None, "promote", data={"allowed": False,
                                             "forced": bool(force and pol_entry),
                                             "reason": reason, "rule": deciding_rule})
        else:
            click.secho(f"DENIED: {reason}", fg="red", err=True)
        ctx_exit(EXIT_POLICY_DENIED)
    if pol_entry and current_output_mode() != "json":
        click.secho(f"Policy PASS: {reason}", fg="green")

    # Land it: switch to the target branch and merge the candidate (real single path).
    # NOTE: merge's own policy hook is bypassed here ON PURPOSE — after checkout, HEAD is
    # the BASELINE tip, so a merge-side re-check would compare the baseline against
    # itself and falsely deny strict-inequality policies. promote IS the enforcement.
    from .cmd_history import checkout
    from .cmd_sync import merge as merge_cmd

    if resolve_head(repo_root)[0] != into_branch:
        ctx.invoke(checkout, name=into_branch)

    merge_kwargs = dict(
        target=cand_ref,
        message=f"Promote {str(cand_ref)[:7]} into {into_branch}"
                + (" [policy bypassed]" if force and pol_entry else ""),
        policy_ours=False, policy_theirs=False, no_ff=True,
        force=bool(pol_entry),  # enforcement already happened above
    )

    if current_output_mode() != "json":
        ctx.invoke(merge_cmd, **merge_kwargs)
        return

    # merge_cmd emits its own top-level JSON envelope -- capture its output instead of
    # letting it reach real stdout, and fold the parsed envelope into promote's own single
    # combined one. On a merge failure, forward its captured envelope verbatim and re-raise.
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ctx.invoke(merge_cmd, **merge_kwargs)
    except SystemExit:
        click.echo(buf.getvalue(), nl=False)
        raise

    merge_line = buf.getvalue().strip().splitlines()[-1] if buf.getvalue().strip() else "{}"
    merge_data = json.loads(merge_line).get("data")
    emit_json(None, "promote", data={"allowed": True, "forced": bool(force and pol_entry),
                                     "reason": reason, "rule": deciding_rule,
                                     "merge": merge_data})


def ctx_exit(code):
    raise SystemExit(code)


# ---------------------------------------------------------------------------
# Signed policy packs — an append-only, hash-chained publication log for promotion-rule
# changes. Separate from `.av/policies.json` (the local, mutable, currently-armed rules);
# a policy pack is a published snapshot with a tamper-evident history.
# ---------------------------------------------------------------------------

def _client(repo_root):
    from .client import VaultClient
    from .core import resolve_remote

    return VaultClient(*resolve_remote(repo_root))


def _pack_require_online(repo_root):
    """Same reachability contract every other read/write against the registry uses --
    an unguarded `client.session.get/post` would otherwise raise a raw ConnectionError
    instead of the documented `unreachable_queued`/13."""
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued",
             f"Registry unreachable at {client.server_url} — policy packs are server-"
             "authoritative.")
    return client


@click.group("pack")
def policy_pack() -> None:
    """Signed, hash-chained, append-only publication log for promotion-rule changes."""


@policy_pack.command("publish")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--sign/--no-sign", default=True, show_default=True)
def pack_publish(file: str, sign: bool) -> None:
    """Publish FILE (any JSON document — typically .av/policies.json and/or
    .av/improver_policy.json) as the next entry on this project's policy-pack chain."""
    from . import casobj
    from .cmd_freeze import freeze_guard

    repo_root = ensure_repo()
    freeze_guard(repo_root)
    try:
        doc = json.loads(Path(file).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(None, "validation", f"{file} is not valid JSON: {exc}")
    if not isinstance(doc, dict):
        fail(None, "validation", "Policy pack document must be a JSON object.")

    if sign:
        sig = casobj.sign_object(doc, repo_root)
        if sig:
            doc["signature"] = sig
    object_id = casobj.write_object(repo_root, doc)

    client = _pack_require_online(repo_root)
    if not client.upload_object(casobj.object_path(repo_root, object_id), object_id):
        fail(None, "unreachable_queued", "Failed to upload the policy pack object.")

    cfg = load_config(repo_root)
    prev = (client.session.get(f"{client.server_url}/api/policy-packs/latest",
                               params={"project_id": cfg["project_id"]}))
    prev_id = prev.json().get("id") if prev.status_code == 200 else None

    resp = client.session.post(f"{client.server_url}/api/policy-packs", json={
        "project_id": cfg["project_id"], "object_id": object_id, "prev_id": prev_id,
    })
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Registry rejected the policy pack: {resp.text[:200]}")
    body = resp.json()

    if current_output_mode() == "json":
        emit_json(None, "policy pack publish", data={**body, "object_id": object_id,
                                                      "prev_id": prev_id, "signed": sign and "signature" in doc})
        return
    click.secho(f"Policy pack {body['id']} published (prev: {(prev_id or '-')[:8]})"
               + (", signed" if sign and "signature" in doc else ""), fg="green")


@policy_pack.command("show")
@click.argument("pack_id")
def pack_show(pack_id: str) -> None:
    """Show one policy pack's content and chain metadata."""
    from . import casobj

    repo_root = ensure_repo()
    client = _pack_require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/policy-packs/{pack_id}")
    if resp.status_code != 200:
        fail(None, "validation", f"Unknown policy pack: {pack_id}")
    row = resp.json()
    doc = casobj.read_object(repo_root, row["object_id"])
    if doc is None:
        client.download_object(row["object_id"], casobj.object_path(repo_root, row["object_id"]))
        doc = casobj.read_object(repo_root, row["object_id"])
    if current_output_mode() == "json":
        emit_json(None, "policy pack show", data={**row, "document": doc})
        return
    click.secho(f"Policy pack {row['id']}", bold=True)
    click.echo(f"  prev: {row.get('prev_id') or '-'}")
    click.echo(f"  chain_hash: {row.get('chain_hash')}")
    click.echo(f"  published_by: {row.get('published_by') or '-'} at {row.get('created_at')}")
    if doc:
        click.echo(f"  signed: {'signature' in doc}")


@policy_pack.command("log")
@click.option("--project", "project_id", default=None, help="Defaults to this repo's project.")
@click.option("--limit", default=20, show_default=True)
def pack_log(project_id: str | None, limit: int) -> None:
    """List this project's policy-pack chain, newest first."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _pack_require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/policy-packs",
                              params={"project_id": project_id or cfg.get("project_id"),
                                      "limit": limit})
    rows = resp.json().get("policy_packs", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "policy pack log", data={"policy_packs": rows})
        return
    if not rows:
        click.secho("No policy packs published yet.", fg="yellow")
        return
    for r in rows:
        click.echo(f"  {r['id'][:8]}  prev={((r.get('prev_id') or '-')[:8])}  {r.get('created_at', '')}")


@policy_pack.command("verify")
@click.argument("pack_id")
def pack_verify(pack_id: str) -> None:
    """Verify one policy pack's chain hash and (if present) its signature."""
    import hashlib as _hashlib

    from . import casobj

    repo_root = ensure_repo()
    client = _pack_require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/policy-packs/{pack_id}")
    if resp.status_code != 200:
        fail(None, "validation", f"Unknown policy pack: {pack_id}")
    row = resp.json()
    doc = casobj.read_object(repo_root, row["object_id"])
    if doc is None:
        client.download_object(row["object_id"], casobj.object_path(repo_root, row["object_id"]))
        doc = casobj.read_object(repo_root, row["object_id"])

    expected_chain = _hashlib.sha256(
        f"{row.get('prev_id') or ''}:{row['object_id']}".encode()
    ).hexdigest()
    chain_ok = expected_chain == row.get("chain_hash")

    sig_ok, sig_reason = (None, "no document to verify")
    if isinstance(doc, dict):
        sig_ok, sig_reason = casobj.verify_object(doc)

    data = {"id": pack_id, "chain_ok": chain_ok, "signature_ok": sig_ok, "reason": sig_reason}
    if current_output_mode() == "json":
        emit_json(None, "policy pack verify", data=data)
        # A broken chain must not exit 0 just because JSON mode already emitted chain_ok: false.
        if not chain_ok:
            ctx_exit(EXIT_VALIDATION)
        return
    click.secho(f"chain: {'OK' if chain_ok else 'BROKEN'}", fg="green" if chain_ok else "red")
    click.secho(f"signature: {sig_reason}", fg="green" if sig_ok else "yellow")
    if not chain_ok:
        ctx_exit(EXIT_VALIDATION)


policy.add_command(policy_pack)
