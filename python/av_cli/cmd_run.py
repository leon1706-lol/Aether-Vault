"""av run — first-class Experiment/Run lifecycle (v1.2.0). A run groups the commits of
one training effort; `av run start` registers server-side when reachable and always
writes local state (.av/run.json) so offline agents keep grouping. AV_RUN_ID overrides/
feeds the same state.
"""

import json
import os
import uuid

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import EXIT_UNREACHABLE_QUEUED, current_output_mode, emit_json, resolve_remote


def _state_path(repo_root):
    return repo_root / ".av" / "run.json"


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


def _register_remote(repo_root, payload: dict) -> tuple[bool, dict | None]:
    """POST /api/runs; returns (registered, response_dict_or_None). Never raises."""
    client = _client(repo_root)
    if not client.server_available():
        return False, None
    try:
        resp = client.session.post(f"{client.server_url}/api/runs", json=payload)
        if resp.status_code == 200:
            return True, resp.json()
    except Exception:
        pass
    return False, None


@click.group()
def run() -> None:
    """Experiment runs: group commits, track lineage and metrics summaries."""


_RUN_KINDS = ("train", "meta", "scoring", "eval")


@run.command()
@click.argument("name", required=False)
@click.option("--parent", "parent_run_id", default=None,
              help="Parent run id (fine-tune descended from that run).")
@click.option("--kind", "kind", type=click.Choice(_RUN_KINDS), default="train", show_default=True,
              help="v1.3.1: 'meta' improves the improver itself (agent code/prompts/"
                   "tools/policy), not the target model — see `av improver`. 'scoring' "
                   "and 'eval' are for the held-out eval vault (`av eval`).")
@click.option("--improver-id", "improver_id", default=None,
              help="v1.3.1: the improver version (see `av improver list`) that authored "
                   "this run, when known.")
def start(name: str | None, parent_run_id: str | None, kind: str, improver_id: str | None) -> None:
    """Start a run; every subsequent commit is filed under it (also via AV_RUN_ID)."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    run_id = str(uuid.uuid4())
    from .core import capture_code_pointer

    code_pointer = capture_code_pointer(repo_root)

    # An already-captured snapshot links to the run at registration AND in local state.
    loaded_snapshot = load_env_snapshot(repo_root)
    env_snapshot_id = loaded_snapshot[0] if loaded_snapshot else None

    # A scoring run's whole point is to be independently reproducible -- one without a
    # pinned environment AND code revision is rejected up front rather than discovered
    # irreproducible later.
    if kind == "scoring":
        missing = []
        if not env_snapshot_id:
            missing.append("an env snapshot (`av env snapshot` first)")
        if not (code_pointer or {}).get("git_sha"):
            missing.append("a pinned code revision (must be a git checkout with a commit)")
        if missing:
            fail(None, "validation",
                 "`--kind scoring` requires " + " and ".join(missing) + " — a scoring "
                 "run must be independently reproducible.")

    registered, _ = _register_remote(repo_root, {
        "id": run_id, "project_id": cfg["project_id"], "name": name,
        "parent_run_id": parent_run_id, "code_pointer": code_pointer,
        "kind": kind, "improver_id": improver_id,
        **({"env_snapshot_id": env_snapshot_id} if env_snapshot_id else {}),
    })

    state = {"run_id": run_id, "name": name, "status": "running",
             "parent_run_ids": [parent_run_id] if parent_run_id else [],
             "code_pointer": code_pointer, "started_at": True,
             "kind": kind, "improver_id": improver_id,
             **({"env_snapshot_id": env_snapshot_id} if env_snapshot_id else {})}
    _state_path(repo_root).write_text(json.dumps(state), encoding="utf-8")

    if current_output_mode() == "json":
        emit_json(None, "run start", data={"run_id": run_id, "name": name, "kind": kind,
                                           "registered_server_side": registered})
        return
    where = "server + local" if registered else "LOCAL ONLY (will register on first push)"
    kind_suffix = f" [{kind}]" if kind != "train" else ""
    click.secho(f"Run {run_id}{kind_suffix} started ({where}). Commits now carry run:{run_id}.",
                fg="green")


@run.command()
@click.option("--fail", "as_failed", is_flag=True, default=False, help="Mark as failed.")
@click.option("--metric", "metrics_raw", multiple=True, help="key=value final metric.")
def finish(metrics_raw: tuple, as_failed: bool) -> None:
    """Complete (or fail) the active run and clear local state."""
    repo_root = ensure_repo()
    path = _state_path(repo_root)
    if not path.exists():
        fail(None, "validation", "No active run — `av run start` first.")
    state = json.loads(path.read_text(encoding="utf-8"))
    run_id = state.get("run_id")

    from .core import parse_metric_args

    summary: dict = parse_metric_args(metrics_raw)

    status = "failed" if as_failed else "completed"
    endpoint = f"/api/runs/{run_id}/{'fail' if as_failed else 'complete'}"
    client = _client(repo_root)
    delivered = False
    if client.server_available():
        try:
            resp = client.session.post(f"{client.server_url}{endpoint}",
                                       json={"metrics_summary": summary})
            delivered = resp.status_code == 200
        except Exception:
            delivered = False

    # Regenerate the handoff HERE, before run state is removed below, since
    # build_handoff_dict() reads the active run_id from that file. Best-effort and never
    # fails the finish itself.
    handoff_written = False
    try:
        from .handoff import generate_handoff

        generate_handoff(repo_root, update=True)
        handoff_written = True
    except Exception:
        handoff_written = False

    path.unlink(missing_ok=True)
    if current_output_mode() == "json":
        emit_json(None, "run finish", data={"run_id": run_id, "status": status,
                                            "metrics_summary": summary,
                                            "delivered_to_registry": delivered,
                                            "handoff_written": handoff_written})
        return
    click.secho(f"Run {run_id} → {status}{' (registry updated)' if delivered else ' (local only)'}",
                fg="green" if not as_failed else "yellow")


@run.command("list")
@click.option("--project", "project_id", default=None, help="Defaults to this repo's project.")
@click.option("--status", "status_filter", default=None)
@click.option("--kind", "kind_filter", type=click.Choice(_RUN_KINDS), default=None,
              help="v1.3.1: filter to one run kind (train/meta/scoring/eval).")
@click.option("--limit", default=20, show_default=True)
def list_runs(project_id: str | None, status_filter: str | None, kind_filter: str | None,
              limit: int) -> None:
    """List runs known to the registry."""
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    pid = project_id or cfg.get("project_id")
    client = _client(repo_root)
    params = {"project_id": pid, "limit": limit}
    if status_filter:
        params["status"] = status_filter
    if kind_filter:
        params["kind"] = kind_filter
    try:
        resp = client.session.get(f"{client.server_url}/api/runs", params=params)
        rows = resp.json().get("runs", []) if resp.status_code == 200 else []
    except Exception:
        rows = []
    if current_output_mode() == "json":
        emit_json(None, "run list", data={"runs": rows})
        return
    if not rows:
        click.secho("No runs on the registry yet.", fg="yellow")
        return
    for r in rows:
        ms = r.get("metrics_summary") or {}
        tail = ", ".join(f"{k}={v}" for k, v in list(ms.items())[:3])
        kind_prefix = f"{r.get('kind', 'train'):>7} " if r.get("kind", "train") != "train" else "        "
        click.echo(f"  [{r['status']:>9}]{kind_prefix}{r['id'][:8]}  {(r.get('name') or '-'):<20} {tail}")


@run.command()
@click.argument("run_id")
def show(run_id: str) -> None:
    """Show one run incl. linked commits."""
    client = _client(ensure_repo())
    try:
        resp = client.session.get(f"{client.server_url}/api/runs/{run_id}")
        body = resp.json()
    except Exception:
        fail(None, "validation", "Registry unreachable or run unknown.")
    if current_output_mode() == "json":
        emit_json(None, "run show", data=body)
        return
    kind_suffix = f" ({body.get('kind')})" if body.get("kind", "train") != "train" else ""
    click.secho(f"Run {body['id']} [{body['status']}]{kind_suffix} {body.get('name') or ''}", bold=True)
    if body.get("improver_id"):
        click.echo(f"  improver: {body['improver_id'][:12]}")
    cp = body.get("code_pointer") or {}
    if cp.get("git_sha"):
        dirty = "dirty" if cp.get("dirty") else "clean"
        click.echo(f"  code: {cp.get('git_remote')}@{str(cp['git_sha'])[:10]} ({dirty})")
    for k, v in (body.get("metrics_summary") or {}).items():
        click.echo(f"  {k}: {v}")
    hashes = body.get("commit_hashes") or []
    click.echo(f"  commits: {len(hashes)}")


_GAP_THRESHOLD = 0.2  # relative difference beyond which a train/eval gap is flagged


@run.command("integrity-check")
@click.argument("run_id")
@click.option("--suite", "suite_id", required=True, help="Eval suite to compare against (av eval).")
def run_integrity_check(run_id: str, suite_id: str) -> None:
    """Compute metric-gaming detection signals (todo.md B.10) for RUN_ID against SUITE_ID's
    most recent (revealed) score, and report them to the registry — best-effort, never a
    gate. Flags a large relative train/eval gap on any metric present in both."""
    repo_root = ensure_repo()
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued", "Registry unreachable — cannot compare against eval results.")

    run_resp = client.session.get(f"{client.server_url}/api/runs/{run_id}")
    if run_resp.status_code != 200:
        fail(None, "validation", f"Unknown run: {run_id}")
    train_metrics = run_resp.json().get("metrics_summary") or {}

    results_resp = client.session.get(f"{client.server_url}/api/eval/results",
                                      params={"suite_id": suite_id, "run_id": run_id})
    rows = results_resp.json().get("eval_results", []) if results_resp.status_code == 200 else []
    revealed = [r for r in rows if r.get("revealed") and isinstance(r.get("score"), dict)]
    eval_metrics = revealed[0]["score"] if revealed else {}

    gaps = {}
    flagged = []
    for metric, train_val in train_metrics.items():
        eval_val = eval_metrics.get(metric)
        if not isinstance(train_val, (int, float)) or not isinstance(eval_val, (int, float)):
            continue
        denom = abs(train_val) if train_val else 1.0
        rel_gap = abs(train_val - eval_val) / denom
        gaps[metric] = {"train": train_val, "eval": eval_val, "relative_gap": rel_gap}
        if rel_gap > _GAP_THRESHOLD:
            flagged.append(metric)

    signals = {
        "suite_id": suite_id, "has_eval_result": bool(revealed),
        "metric_gaps": gaps, "flagged_metrics": flagged,
        "eval_only_improvement": False,  # requires a train-metric time series this CLI
                                          # snapshot doesn't have — left explicit/false
                                          # rather than guessed; see architecture.md
        "data_overlap": None,  # requires the suite to declare dataset object hashes —
                                # not yet modeled; reported honestly as unknown, not 0
    }

    resp = client.session.post(f"{client.server_url}/api/runs/{run_id}/integrity-signals",
                               json={"signals": signals})
    reported = resp.status_code == 200

    if current_output_mode() == "json":
        emit_json(None, "run integrity-check", data={**signals, "reported": reported})
        return
    if flagged:
        click.secho(f"⚠ train/eval gap flagged on: {', '.join(flagged)}", fg="yellow", bold=True)
    else:
        click.secho("No large train/eval gaps detected.", fg="green")
    for metric, g in gaps.items():
        click.echo(f"  {metric}: train={g['train']} eval={g['eval']} (gap {g['relative_gap']:.1%})")


@run.command("stop")
@click.argument("run_id")
@click.option("--reason", default=None,
              help="plateau|divergence|nan|canary_failure|budget, or free text.")
def run_stop(run_id: str, reason: str | None) -> None:
    """External stop (todo.md D.19/D.20) — distinct from `run finish --fail`: this is
    something OUTSIDE the run deciding to end it (a scheduler, an auto-stop check), not
    the training process reporting its own failure."""
    client = _client(ensure_repo())
    if not client.server_available():
        fail(None, "unreachable_queued", "Registry unreachable — cannot stop a remote run.")
    resp = client.session.post(f"{client.server_url}/api/runs/{run_id}/stop",
                               json={"reason": reason})
    if resp.status_code != 200:
        fail(None, "validation", f"Could not stop run {run_id}: {resp.text[:200]}")
    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "run stop", data=body)
        return
    click.secho(f"Run {run_id} stopped" + (f" ({reason})" if reason else "") + ".", fg="yellow")


# ---------------------------------------------------------------------------
# Branch exploration policy (todo.md D.18) — advisory only: recommends branch/merge/
# abandon based on a run's metrics against locally-configured rules. Never takes the
# action itself (`av branch`/`av merge`/`av run stop` remain separate, deliberate calls).
# ---------------------------------------------------------------------------

def _branch_policy_path(repo_root):
    return repo_root / ".av" / "branch_policy.json"


def _load_branch_policy(repo_root) -> dict:
    path = _branch_policy_path(repo_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_rule(rule_str: str) -> dict:
    """"metric op threshold", e.g. "val_loss < 0.5"."""
    from .cmd_policy import _OPS

    parts = rule_str.split()
    if len(parts) != 3 or parts[1] not in _OPS:
        fail(None, "validation",
             f"Malformed rule {rule_str!r} — expected \"METRIC OP THRESHOLD\", "
             f"OP one of {sorted(_OPS)}.")
    metric, op, threshold = parts
    return {"metric": metric, "op": op, "threshold": float(threshold)}


@run.group("branch-policy")
def run_branch_policy() -> None:
    """Advisory branch/merge/abandon recommendations based on a run's metrics."""


@run_branch_policy.command("set")
@click.option("--branch-if", default=None, help='e.g. "val_loss < 0.3"')
@click.option("--merge-if", default=None, help='e.g. "val_loss < 0.1"')
@click.option("--abandon-if", default=None, help='e.g. "val_loss > 2.0"')
def branch_policy_set(branch_if: str | None, merge_if: str | None, abandon_if: str | None) -> None:
    repo_root = ensure_repo()
    if not (branch_if or merge_if or abandon_if):
        fail(None, "validation", "Provide at least one of --branch-if/--merge-if/--abandon-if.")
    pol = {}
    if branch_if:
        pol["branch_if"] = _parse_rule(branch_if)
    if merge_if:
        pol["merge_if"] = _parse_rule(merge_if)
    if abandon_if:
        pol["abandon_if"] = _parse_rule(abandon_if)
    atomic_write_text(_branch_policy_path(repo_root), json.dumps(pol, indent=2, sort_keys=True))
    if current_output_mode() == "json":
        emit_json(None, "run branch-policy set", data=pol)
        return
    click.secho(f"Branch policy armed: {pol}", fg="green")


@run_branch_policy.command("show")
def branch_policy_show() -> None:
    repo_root = ensure_repo()
    pol = _load_branch_policy(repo_root)
    if current_output_mode() == "json":
        emit_json(None, "run branch-policy show", data=pol)
        return
    click.echo(json.dumps(pol, indent=2) if pol else "No branch policy armed.")


@run_branch_policy.command("check")
@click.argument("run_id")
def branch_policy_check(run_id: str) -> None:
    from .cmd_policy import _OPS

    repo_root = ensure_repo()
    pol = _load_branch_policy(repo_root)
    if not pol:
        fail(None, "validation", "No branch policy armed — `av run branch-policy set` first.")
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued", "Registry unreachable — cannot read the run's metrics.")
    resp = client.session.get(f"{client.server_url}/api/runs/{run_id}")
    if resp.status_code != 200:
        fail(None, "validation", f"Unknown run: {run_id}")
    metrics = resp.json().get("metrics_summary") or {}

    matched = []
    for action in ("abandon_if", "merge_if", "branch_if"):  # most consequential first
        rule = pol.get(action)
        if not rule:
            continue
        value = metrics.get(rule["metric"])
        if value is not None and _OPS[rule["op"]](value, rule["threshold"]):
            matched.append(action.replace("_if", ""))

    recommendation = matched[0] if matched else "continue"
    if current_output_mode() == "json":
        emit_json(None, "run branch-policy check",
                 data={"run_id": run_id, "recommendation": recommendation,
                       "matched_rules": matched, "metrics": metrics})
        return
    click.secho(f"Recommendation: {recommendation.upper()}",
               fg="red" if recommendation == "abandon" else "green")


@run.command("auto-stop-check")
@click.argument("run_id")
@click.option("--metric", "metric_name", required=True, help="Metric to monitor, e.g. val_loss.")
@click.option("--minimize/--maximize", default=True, show_default=True,
              help="Whether improvement means the metric going down (loss) or up (accuracy).")
@click.option("--patience", type=int, default=5, show_default=True,
              help="Consecutive non-improving points before flagging a plateau.")
@click.option("--divergence-factor", type=float, default=3.0, show_default=True,
              help="Flag divergence when the latest value exceeds this multiple of the best seen.")
@click.option("--stop/--no-stop", "do_stop", default=False,
              help="Actually call `av run stop` when a condition triggers (default: report only).")
def run_auto_stop_check(run_id: str, metric_name: str, minimize: bool, patience: int,
                        divergence_factor: float, do_stop: bool) -> None:
    """Plateau/divergence/NaN detection over RUN_ID's committed METRIC history (todo.md
    D.19) — a one-shot check an external loop (`av watch`, a scheduler) re-runs
    periodically; not a daemon itself. Reuses the existing uncapped per-commit metric
    series (`GET /api/runs/{id}/metrics`, v1.3.0) rather than tracking a second copy of
    training history anywhere."""
    import math

    repo_root = ensure_repo()
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued", "Registry unreachable — cannot read metric history.")

    points = []
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = client.session.get(f"{client.server_url}/api/runs/{run_id}/metrics", params=params)
        if resp.status_code != 200:
            fail(None, "validation", f"Unknown run: {run_id}")
        body = resp.json()
        points.extend(body.get("points", []))
        cursor = body.get("next_cursor")
        if not cursor or not body.get("points"):
            break

    values = [p["metrics"].get(metric_name) for p in points if metric_name in (p.get("metrics") or {})]

    reason = None
    numeric = [v for v in values if isinstance(v, (int, float))]
    if any(isinstance(v, float) and math.isnan(v) for v in values):
        reason = "nan"
    elif numeric:
        best = min(numeric) if minimize else max(numeric)
        latest = numeric[-1]
        # "Worse than the best seen by more than divergence_factor times the best's own
        # scale" — scale falls back to 1.0 when best is ~0 so a loss that legitimately
        # bottomed out near zero doesn't make EVERY later value look like divergence.
        scale = max(abs(best), 1.0)
        if minimize and latest > best + divergence_factor * scale:
            reason = "divergence"
        elif not minimize and latest < best - divergence_factor * scale:
            reason = "divergence"
        else:
            tail = numeric[-patience:]
            if len(tail) >= patience:
                before_tail = numeric[:-patience] or [tail[0]]
                reference = min(before_tail) if minimize else max(before_tail)
                improved = any((v < reference) if minimize else (v > reference) for v in tail)
                if not improved:
                    reason = "plateau"

    stopped = False
    if reason and do_stop:
        stop_resp = client.session.post(f"{client.server_url}/api/runs/{run_id}/stop",
                                        json={"reason": reason})
        stopped = stop_resp.status_code == 200

    if current_output_mode() == "json":
        emit_json(None, "run auto-stop-check",
                 data={"run_id": run_id, "metric": metric_name, "points_seen": len(values),
                       "triggered": reason, "stopped": stopped})
        return
    if reason:
        click.secho(f"⚠ auto-stop condition met: {reason}" + (" (stopped)" if stopped else ""),
                   fg="red", bold=True)
    else:
        click.secho("No auto-stop condition met.", fg="green")


def current_run_id(repo_root) -> str | None:
    """The active run id, if any (used by commit's auto-tagging). Delegates to
    core.resolve_run_id() -- kept as a thin wrapper so existing imports of this name
    keep working."""
    from .core import resolve_run_id

    return resolve_run_id(repo_root)
