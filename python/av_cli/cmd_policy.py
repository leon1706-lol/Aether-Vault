"""av branch protect / av promote — promotion guardrails for autonomous loops (v1.2.0).

Policies live in .av/policies.json: {"<branch>": {"metric": str, "op": "<"|"<="|">"|">=",
"baseline_ref": str, "require_signature": bool}}. `metric"/"op"` and `require_signature`
(v1.2.5) are independent gates — either or both may be present; a policy with only
`require_signature` denies an unsigned candidate regardless of metrics. Enforcement is
CLIENT-SIDE at merge/push-to-protected-branch time (server-side authz is an enterprise-tier
item); `av promote` evaluates the metric policy by comparing the candidate's latest metrics
against the baseline ref's tip metrics, and (v1.2.5) the signature policy by verifying the
candidate's own embedded signature — tamper evidence, not a PKI; this does not bind a key
to an identity.
"""

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


def _latest_metrics_for_ref(repo_root, ref_name: str | None) -> dict | None:
    """Walks from ref tip backwards, returning the first commit with non-empty metrics.

    `ref_name` may be a branch name (refs/heads lookup) or a raw commit hash — both are
    supported; anything else falls back to HEAD.
    """
    from .handoff import load_commit, resolve_head

    cur = None
    if ref_name in (None, "", "HEAD"):
        cur = resolve_head(repo_root)[1]
    else:
        p = repo_root / ".av" / "refs" / "heads" / ref_name
        if p.exists():
            cur = p.read_text().strip()
        else:
            cur = ref_name  # treat as a commit hash
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        c = load_commit(repo_root, cur)
        if not c:
            break
        m = c.get("metrics") or {}
        if isinstance(m, dict) and m:
            return m
        cur = c.get("parent_hash")
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
    """v1.2.5: for `require_signature` policies — verifies the candidate's OWN embedded
    signature (not a detached one; the candidate must carry it itself to promote/merge).
    Returns (signed_and_valid, reason) so a denial message can say WHY."""
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


def enforce_policy(repo_root, target_branch: str, candidate_metrics: dict | None,
                   baseline_metrics_fn, candidate_ref=None) -> None:
    """Raises PolicyDenied (via fail) when TARGET_BRANCH is protected and policy fails.

    v1.2.5: `candidate_ref` (optional — pass the commit being merged/promoted) enables
    the `require_signature` policy field. Omitting it preserves exact pre-1.2.5 behavior
    for policies that don't set it (existing policies never fail a new, unrelated check)."""
    pol = load_policies(repo_root).get(target_branch)
    if not pol:
        return
    if pol.get("require_signature"):
        signed, sig_reason = candidate_is_signed(repo_root, candidate_ref)
        if not signed:
            fail(None, "policy_denied",
                 f"promotion to '{target_branch}' denied: require_signature is armed and "
                 f"the candidate is not validly signed ({sig_reason}). "
                 "Sign it (`av registry keygen` then re-commit) or override consciously "
                 "with --force.")
    # v1.2.5: require_signature is usable standalone — a policy with no "metric" key is a
    # signature-only gate, not an incomplete metric policy (matches promote()'s handling).
    if not pol.get("metric"):
        return
    base_ref = pol.get("baseline_ref")
    baseline = baseline_metrics_fn(base_ref) if base_ref else None
    ok, reason = evaluate(pol, candidate_metrics, baseline)
    if not ok:
        fail(None, "policy_denied",
             f"promotion to '{target_branch}' denied: {reason}. "
             "Override consciously with --force.")


@click.command()
@click.argument("candidate", required=False, default=None)
@click.option("--into", "into_branch", default="main", show_default=True)
@click.option("--force", is_flag=True, default=False, help="Bypass the armed policy explicitly.")
@click.pass_context
def promote(ctx, candidate: str | None, into_branch: str, force: bool) -> None:
    """Evaluate INTO_BRANCH's armed policy against CANDIDATE's latest metrics, then land it."""
    repo_root = ensure_repo()
    pol_entry = load_policies(repo_root).get(into_branch)

    from .handoff import load_commit, resolve_head

    cand_ref = candidate or resolve_head(repo_root)[1]
    cand_commit = load_commit(repo_root, cand_ref) if cand_ref else None
    if cand_commit is None:
        fail(None, "validation", f"Unknown candidate: {candidate}")
    cand_metrics = cand_commit.get("metrics")

    allowed, reason = True, "no policy armed"
    if pol_entry and not force:
        # v1.2.5: require_signature is checked FIRST — a denial here should say "unsigned",
        # not a misleading metric-comparison message when the metrics happen to also fail.
        if pol_entry.get("require_signature"):
            signed, sig_reason = candidate_is_signed(repo_root, cand_ref)
            if not signed:
                allowed, reason = False, (
                    f"require_signature is armed and the candidate is not validly signed "
                    f"({sig_reason})"
                )
        # v1.2.5: require_signature is usable standalone — a policy with no "metric" key
        # is a signature-only gate, not an incomplete metric policy. evaluate() runs only
        # when a metric IS configured (existing metric-only policies are unaffected).
        if allowed and pol_entry.get("metric"):
            base_ref = pol_entry.get("baseline_ref")
            baseline = _latest_metrics_for_ref(repo_root, base_ref) if base_ref else None
            allowed, reason = evaluate(pol_entry, cand_metrics, baseline)
        elif allowed and pol_entry.get("require_signature"):
            reason = "require_signature: candidate is validly signed"
    elif force and pol_entry:
        reason = f"policy BYPASSED via --force on '{into_branch}'"

    if current_output_mode() == "json":
        emit_json(None, "promote", data={"allowed": allowed, "forced": bool(force and pol_entry),
                                         "reason": reason})
    if not allowed:
        click.secho(f"DENIED: {reason}", fg="red", err=True)
        ctx_exit(EXIT_POLICY_DENIED)
    if pol_entry:
        click.secho(f"Policy PASS: {reason}", fg="green")

    # Land it: switch to the target branch and merge the candidate (real single path).
    # NOTE: merge's own policy hook is bypassed here ON PURPOSE — after checkout, HEAD is
    # the BASELINE tip, so a merge-side re-check would compare the baseline against
    # itself and falsely deny strict-inequality policies. promote IS the enforcement.
    from .cmd_history import checkout
    from .cmd_sync import merge as merge_cmd

    if resolve_head(repo_root)[0] != into_branch:
        ctx.invoke(checkout, name=into_branch)
    ctx.invoke(merge_cmd, target=cand_ref,
               message=f"Promote {str(cand_ref)[:7]} into {into_branch}"
                       + (" [policy bypassed]" if force and pol_entry else ""),
               policy_ours=False, policy_theirs=False, no_ff=True,
               force=bool(pol_entry))  # enforcement already happened above


def ctx_exit(code):
    raise SystemExit(code)
