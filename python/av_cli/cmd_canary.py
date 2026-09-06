"""av canary — capability canaries (v1.3.1): a small, fixed set of metric-threshold checks
that must not regress, evaluated against HEAD's metrics using the same comparison
primitives `cmd_policy.py`'s model-gate policies use. Suites are content-addressed
(`casobj.py`); `.av/canaries.json` maps a human name to its suite's CAS object id.
"""
import datetime
import json

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, resolve_remote


def _registry_path(repo_root):
    return repo_root / ".av" / "canaries.json"


def _load_registry(repo_root) -> dict:
    path = _registry_path(repo_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_registry(repo_root, reg: dict) -> None:
    atomic_write_text(_registry_path(repo_root), json.dumps(reg, indent=2, sort_keys=True))


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


def _require_online(repo_root):
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued",
             f"Registry unreachable at {client.server_url} — canary results are server-"
             "authoritative.")
    return client


@click.group()
def canary() -> None:
    """Capability canaries: fixed checks that must not regress before an improver promotes."""


@canary.command("register")
@click.argument("name")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
def canary_register(name: str, file: str) -> None:
    """Register NAME as a canary suite loaded from FILE (JSON: {"checks": [...]})."""
    from . import casobj

    repo_root = ensure_repo()
    try:
        suite = json.loads(Path(file).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(None, "validation", f"{file} is not valid JSON: {exc}")
    if not isinstance(suite, dict) or not isinstance(suite.get("checks"), list) or not suite["checks"]:
        fail(None, "validation", 'Suite must be {"checks": [{"name","metric","op",'
                                 '"threshold"|"baseline_ref"}, ...]} with at least one check.')
    suite.setdefault("kind", "canary_suite")
    suite.setdefault("name", name)

    object_id = casobj.write_object(repo_root, suite)
    reg = _load_registry(repo_root)
    reg[name] = object_id
    _save_registry(repo_root, reg)

    if current_output_mode() == "json":
        emit_json(None, "canary register", data={"name": name, "object_id": object_id,
                                                  "checks": len(suite["checks"])})
        return
    click.secho(f"Canary suite '{name}' registered ({len(suite['checks'])} checks).", fg="green")


@canary.command("list")
def canary_list() -> None:
    """List locally registered canary suites."""
    repo_root = ensure_repo()
    reg = _load_registry(repo_root)
    if current_output_mode() == "json":
        emit_json(None, "canary list", data={"suites": reg})
        return
    if not reg:
        click.secho("No canary suites registered — `av canary register NAME FILE`.", fg="yellow")
        return
    for name, oid in sorted(reg.items()):
        click.echo(f"  {name}: {oid[:12]}")


@canary.command("run")
@click.argument("name")
@click.option("--improver", "improver_id", default=None,
              help="Improver version this canary run is FOR (defaults to the current pointer).")
def canary_run(name: str, improver_id: str | None) -> None:
    """Evaluate NAME's checks against HEAD's metrics and report the result to the registry."""
    from . import casobj
    from .cmd_improver import current_improver_id
    from .cmd_policy import _OPS
    from .handoff import load_commit, resolve_head

    repo_root = ensure_repo()
    reg = _load_registry(repo_root)
    object_id = reg.get(name)
    if not object_id:
        fail(None, "validation", f"Unknown canary suite: {name} — `av canary register` first.")
    suite = casobj.read_object(repo_root, object_id)
    if suite is None:
        fail(None, "validation", f"Canary suite object {object_id} is missing locally.")

    _, head_hash = resolve_head(repo_root)
    head_commit = load_commit(repo_root, head_hash) if head_hash else None
    metrics = (head_commit or {}).get("metrics") or {}

    results = []
    all_passed = True
    for check in suite["checks"]:
        metric, op = check.get("metric"), check.get("op", "<")
        value = metrics.get(metric)
        threshold = check.get("threshold")
        ok = value is not None and op in _OPS and threshold is not None and _OPS[op](value, threshold)
        all_passed = all_passed and ok
        results.append({"name": check.get("name", metric), "metric": metric, "op": op,
                        "threshold": threshold, "value": value, "passed": ok})

    improver_id = improver_id or current_improver_id(repo_root)
    client = _client(repo_root)
    reported = False
    if improver_id and client.server_available():
        if client.upload_object(casobj.object_path(repo_root, object_id), object_id):
            cfg = load_config(repo_root)
            resp = client.session.post(f"{client.server_url}/api/canary-results", json={
                "project_id": cfg["project_id"], "improver_id": improver_id,
                "suite_object_id": object_id, "passed": all_passed,
                "details": {"checks": results},
            })
            reported = resp.status_code in (200, 201)

    if current_output_mode() == "json":
        emit_json(None, "canary run", data={"name": name, "passed": all_passed,
                                            "checks": results, "reported": reported,
                                            "improver_id": improver_id})
        # Both output modes share the same exit-code consequence -- a failed canary must
        # not exit 0 just because JSON mode already emitted passed:false.
        if not all_passed:
            ctx_exit(EXIT_VALIDATION)
        return
    color = "green" if all_passed else "red"
    click.secho(f"Canary '{name}': {'PASS' if all_passed else 'FAIL'}", fg=color, bold=True)
    for r in results:
        click.echo(f"  [{'ok' if r['passed'] else 'FAIL'}] {r['name']}: "
                  f"{r['value']} {r['op']} {r['threshold']}")
    if not all_passed:
        ctx_exit(EXIT_VALIDATION)


@canary.command("status")
@click.option("--improver", "improver_id", default=None,
              help="Defaults to the current improver pointer.")
@click.option("--project", "project_id", default=None)
def canary_status(improver_id: str | None, project_id: str | None) -> None:
    """Show the most recent canary result for an improver version."""
    from .cmd_improver import current_improver_id

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    improver_id = improver_id or current_improver_id(repo_root)
    if not improver_id:
        fail(None, "validation", "No improver id given and no current pointer set.")
    client = _require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/canary-results",
                              params={"project_id": project_id or cfg.get("project_id"),
                                      "improver_id": improver_id, "limit": 1})
    rows = resp.json().get("canary_results", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "canary status", data={"latest": rows[0] if rows else None})
        return
    if not rows:
        click.secho(f"No canary results recorded for improver {improver_id[:8]}.", fg="yellow")
        return
    r = rows[0]
    click.secho(f"{'PASS' if r['passed'] else 'FAIL'} — {r.get('created_at', '')}",
               fg="green" if r["passed"] else "red", bold=True)


def ctx_exit(code):
    raise SystemExit(code)


def latest_canary_passed(repo_root, client, project_id: str, improver_id: str) -> bool:
    """Used by `av improver promote`'s dual gate -- True only when the most recent canary
    result for this improver version exists and passed."""
    resp = client.session.get(f"{client.server_url}/api/canary-results",
                              params={"project_id": project_id, "improver_id": improver_id,
                                      "limit": 1})
    if resp.status_code != 200:
        return False
    rows = resp.json().get("canary_results", [])
    return bool(rows) and bool(rows[0].get("passed"))
