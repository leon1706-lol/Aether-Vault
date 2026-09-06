"""av eval — task/eval registry, integrity locks, held-out vault, blind scoring, external
adapters (v1.3.1). A suite definition is a CAS object (`casobj.py`); `POST /api/eval/results`
requires the `scorer` scope server-side, which is the held-out vault's actual enforcement,
not just convention. Blind suites create results with `revealed=False` until `av eval reveal`.
"""
import json

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json, resolve_remote


def _client(repo_root):
    from .client import VaultClient

    return VaultClient(*resolve_remote(repo_root))


def _require_online(repo_root):
    client = _client(repo_root)
    if not client.server_available():
        fail(None, "unreachable_queued",
             f"Registry unreachable at {client.server_url} — the eval registry is server-"
             "authoritative.")
    return client


@click.group("eval")
def eval_group() -> None:
    """Task/eval registry: frozen suites, the held-out vault, blind scoring, adapters."""


@eval_group.command("register")
@click.argument("name")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--blind", is_flag=True, default=False,
              help="Scores against this suite stay hidden from non-scorer readers until revealed.")
def eval_register(name: str, file: str, blind: bool) -> None:
    """Register NAME as an eval suite loaded from FILE."""
    from . import casobj

    repo_root = ensure_repo()
    client = _require_online(repo_root)
    try:
        suite = json.loads(Path(file).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(None, "validation", f"{file} is not valid JSON: {exc}")
    object_id = casobj.write_object(repo_root, suite)
    if not client.upload_object(casobj.object_path(repo_root, object_id), object_id):
        fail(None, "unreachable_queued", "Failed to upload the eval suite object.")

    cfg = load_config(repo_root)
    resp = client.session.post(f"{client.server_url}/api/eval/suites", json={
        "project_id": cfg["project_id"], "object_id": object_id, "name": name, "blind": blind,
    })
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Registry rejected the eval suite: {resp.text[:200]}")
    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "eval register", data={**body, "object_id": object_id, "blind": blind})
        return
    click.secho(f"Eval suite '{name}' registered ({body['id'][:8]})"
               + (", blind" if blind else ""), fg="green")


@eval_group.command("list")
@click.option("--project", "project_id", default=None)
def eval_list(project_id: str | None) -> None:
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/eval/suites",
                              params={"project_id": project_id or cfg.get("project_id")})
    rows = resp.json().get("suites", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "eval list", data={"suites": rows})
        return
    for r in rows:
        flags = " ".join(f for f, v in (("frozen", r["frozen"]), ("blind", r["blind"])) if v)
        click.echo(f"  {r['id'][:8]}  {r.get('name') or '-':<20} {flags}")


@eval_group.command("show")
@click.argument("suite_id")
def eval_show(suite_id: str) -> None:
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/eval/suites/{suite_id}")
    if resp.status_code != 200:
        fail(None, "validation", f"Unknown eval suite: {suite_id}")
    row = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "eval show", data=row)
        return
    click.secho(f"Eval suite {row['id']}: {row.get('name') or '-'}", bold=True)
    click.echo(f"  frozen: {row['frozen']}   blind: {row['blind']}")


@eval_group.command("freeze")
@click.argument("suite_id")
def eval_freeze(suite_id: str) -> None:
    """Freeze SUITE_ID — no route may mutate it again (todo.md B.7)."""
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.post(f"{client.server_url}/api/eval/suites/{suite_id}/freeze")
    if resp.status_code == 403:
        fail(None, "scope_denied", "Token lacks the 'eval:write' scope required to freeze a suite.")
    if resp.status_code != 200:
        fail(None, "validation", f"Could not freeze {suite_id}: {resp.text[:200]}")
    if current_output_mode() == "json":
        emit_json(None, "eval freeze", data=resp.json())
        return
    click.secho(f"Eval suite {suite_id} frozen.", fg="green")


@eval_group.command("score")
@click.argument("suite_id")
@click.option("--run", "run_id", default=None)
@click.option("--metric", "metrics_raw", multiple=True, help="key=value score component.")
def eval_score(suite_id: str, run_id: str | None, metrics_raw: tuple) -> None:
    """Record a score against SUITE_ID — requires the `scorer` scope server-side."""
    from .core import parse_metric_args

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    score = parse_metric_args(metrics_raw)
    resp = client.session.post(f"{client.server_url}/api/eval/results", json={
        "project_id": cfg["project_id"], "suite_id": suite_id, "run_id": run_id, "score": score,
    })
    if resp.status_code == 403:
        fail(None, "scope_denied", "Token lacks the 'scorer' scope required to record a score.")
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Registry rejected the score: {resp.text[:200]}")
    body = resp.json()
    if current_output_mode() == "json":
        emit_json(None, "eval score", data=body)
        return
    click.secho(f"Score recorded ({body['id']})"
               + (" — hidden until revealed" if not body.get("revealed") else ""), fg="green")


@eval_group.command("results")
@click.option("--suite", "suite_id", default=None)
@click.option("--run", "run_id", default=None)
@click.option("--project", "project_id", default=None)
def eval_results(suite_id: str | None, run_id: str | None, project_id: str | None) -> None:
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    params = {"project_id": project_id or cfg.get("project_id")}
    if suite_id:
        params["suite_id"] = suite_id
    if run_id:
        params["run_id"] = run_id
    resp = client.session.get(f"{client.server_url}/api/eval/results", params=params)
    rows = resp.json().get("eval_results", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "eval results", data={"eval_results": rows})
        return
    for r in rows:
        shown = r["score"] if r["revealed"] else "(hidden)"
        click.echo(f"  #{r['id']}  suite={r['suite_id'][:8]}  score={shown}")


@eval_group.command("reveal")
@click.argument("result_id", type=int)
def eval_reveal(result_id: int) -> None:
    repo_root = ensure_repo()
    client = _require_online(repo_root)
    resp = client.session.post(f"{client.server_url}/api/eval/results/{result_id}/reveal")
    if resp.status_code == 403:
        fail(None, "scope_denied", "Token lacks the 'scorer' scope required to reveal a result.")
    if resp.status_code != 200:
        fail(None, "validation", f"Could not reveal result {result_id}: {resp.text[:200]}")
    if current_output_mode() == "json":
        emit_json(None, "eval reveal", data=resp.json())
        return
    click.secho(f"Result #{result_id} revealed: {resp.json()['score']}", fg="green")


@eval_group.group("adapter")
def eval_adapter() -> None:
    """External eval adapters: subprocess contract, JSON stdin -> JSON stdout."""


@eval_adapter.command("add")
@click.argument("name")
@click.option("--command", "command_str", required=True,
              help="Shell-quoted command, e.g. --command \"python score.py\".")
def adapter_add(name: str, command_str: str) -> None:
    import shlex

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    command = shlex.split(command_str)
    resp = client.session.post(f"{client.server_url}/api/eval/adapters", json={
        "project_id": cfg["project_id"], "name": name, "command": command,
    })
    if resp.status_code not in (200, 201):
        fail(None, "validation", f"Registry rejected the adapter: {resp.text[:200]}")
    if current_output_mode() == "json":
        emit_json(None, "eval adapter add", data=resp.json())
        return
    click.secho(f"Adapter '{name}' registered.", fg="green")


@eval_adapter.command("list")
@click.option("--project", "project_id", default=None)
def adapter_list(project_id: str | None) -> None:
    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/eval/adapters",
                              params={"project_id": project_id or cfg.get("project_id")})
    rows = resp.json().get("adapters", []) if resp.status_code == 200 else []
    if current_output_mode() == "json":
        emit_json(None, "eval adapter list", data={"adapters": rows})
        return
    for r in rows:
        click.echo(f"  {r['name']}: {' '.join(r['command'])}")


@eval_adapter.command("run")
@click.argument("name")
@click.option("--input", "input_file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="JSON file piped to the adapter's stdin (default: {}).")
@click.option("--project", "project_id", default=None)
def adapter_run(name: str, input_file: str | None, project_id: str | None) -> None:
    """Run adapter NAME: JSON on stdin, JSON on stdout, non-zero exit = failed scoring —
    success cannot be silently redefined by whatever's currently checked in-tree."""
    import subprocess

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = _require_online(repo_root)
    resp = client.session.get(f"{client.server_url}/api/eval/adapters",
                              params={"project_id": project_id or cfg.get("project_id")})
    rows = resp.json().get("adapters", []) if resp.status_code == 200 else []
    adapter = next((r for r in rows if r["name"] == name), None)
    if not adapter:
        fail(None, "validation", f"Unknown adapter: {name}")

    stdin_payload = Path(input_file).read_text(encoding="utf-8") if input_file else "{}"
    try:
        proc = subprocess.run(adapter["command"], input=stdin_payload, capture_output=True,
                              text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(None, "validation", f"Adapter '{name}' failed to run: {exc}")
    if proc.returncode != 0:
        fail(None, "validation",
             f"Adapter '{name}' exited {proc.returncode} (failed scoring): {proc.stderr[:300]}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        fail(None, "validation", f"Adapter '{name}' did not print valid JSON on stdout.")

    if current_output_mode() == "json":
        emit_json(None, "eval adapter run", data={"name": name, "result": result})
        return
    click.echo(json.dumps(result, indent=2))
