"""av context — the agent context-memory surface (v1.2.0).

Notes are append-only and durable (.av/context/memory.jsonl), so successive agents
inherit predecessor intent; export renders the full .avh v2 document (or slices).
"""

import datetime
import json
import pathlib

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import current_output_mode, emit_json


def _memory_path(repo_root):
    return repo_root / ".av" / "context" / "memory.jsonl"


@click.group()
def context() -> None:
    """Agent context memory: notes, validation, diffing, and export of the .avh."""


@context.command()
@click.argument("note")
@click.option("--agent", default=None, help="Who wrote this note (defaults to AV_AUTHOR).")
def note(note: str, agent: str | None) -> None:
    """Append an immutable note to the repo's agent memory."""
    import os

    repo_root = ensure_repo()
    path = _memory_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # v1.3.0 (todo.md item 8): stamp the active run (if any) at write time — additive
    # field, so `av context search --run ID` can scope notes to the run they were
    # written under. resolve_run_id() is the same single precedence rule every other
    # commit path already shares (explicit > AV_RUN_ID env > .av/run.json state).
    entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent": agent or os.environ.get("AV_AUTHOR", "anonymous"),
        "note": note,
        "run_id": resolve_run_id(repo_root),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    if current_output_mode() == "json":
        emit_json(None, "context note", data={"appended": True, "entry": entry})
        return
    click.secho(f"Noted ({len(entry['note'])} chars). Memory now carries the note forward.", fg="green")


@context.command()
@click.argument("query")
@click.option("--run", "run_id_filter", default=None, help="Only notes written under this run id.")
@click.option("--since", "since_ts", default=None,
              help="Only notes at/after this ISO-8601 timestamp.")
@click.option("--case-sensitive", is_flag=True, default=False)
def search(query: str, run_id_filter: str | None, since_ts: str | None, case_sensitive: bool) -> None:
    """Search context-memory notes by substring (todo.md item 8: "notes mentioning X").

    Plain substring match over each note's text — no full-text index, this is a small
    append-only JSONL file per repo, not a search-engine-scale corpus. --run and --since
    narrow first; QUERY then filters by substring within whatever's left.
    """
    from .handoff import _context_notes

    repo_root = ensure_repo()
    notes = _context_notes(repo_root)

    if run_id_filter:
        notes = [n for n in notes if n.get("run_id") == run_id_filter]
    if since_ts:
        notes = [n for n in notes if (n.get("ts") or "") >= since_ts]

    needle = query if case_sensitive else query.lower()

    def _matches(n: dict) -> bool:
        haystack = n.get("note") or ""
        return needle in (haystack if case_sensitive else haystack.lower())

    matches = [n for n in notes if _matches(n)]

    if current_output_mode() == "json":
        emit_json(None, "context search", data={"query": query, "matches": matches,
                                                 "count": len(matches)})
        return
    if not matches:
        click.secho(f"No notes match {query!r}.", fg="yellow")
        return
    for n in matches:
        who = n.get("agent") or "?"
        when = (n.get("ts") or "")[:19]
        run_suffix = f"  [run {n['run_id'][:8]}…]" if n.get("run_id") else ""
        click.echo(f"[{when}] {who}: {n.get('note')}{run_suffix}")


@context.command()
def show() -> None:
    """Print all remembered notes, oldest first."""
    from .handoff import _context_notes

    repo_root = ensure_repo()
    notes = _context_notes(repo_root)
    if current_output_mode() == "json":
        emit_json(None, "context show", data={"notes": notes})
        return
    if not notes:
        click.secho("No notes yet — `av context note \"...\"` starts the memory.", fg="yellow")
        return
    for n in notes:
        who = n.get("agent") or "?"
        when = (n.get("ts") or "")[:19]
        click.echo(f"[{when}] {who}: {n.get('note')}")


@context.command()
def validate() -> None:
    """Structurally validate handoff.avh against the v2 shape."""
    from .handoff import upgrade_handoff, validate_handoff

    repo_root = ensure_repo()
    avh_path = repo_root / "handoff.avh"
    if not avh_path.exists():
        fail(None, "validation", "handoff.avh not found — run `av handoff` first.")
    doc = json.loads(avh_path.read_text(encoding="utf-8"))
    problems = validate_handoff(upgrade_handoff(doc))
    if current_output_mode() == "json":
        emit_json(None, "context validate", data={"valid": not problems, "problems": problems})
        return
    if problems:
        for p in problems:
            click.secho(f"  problem: {p}", fg="red")
        ctx_exit(1)
    click.secho("handoff.avh matches the v2 structural contract.", fg="green")


@context.command()
@click.option("--against", "against_path", default=None,
              help="Compare against this .avh file (default: the Aether-Handoff snapshot before latest).")
def diff(against_path: str | None) -> None:
    """Diff the current .avh v2 document against a previous one (sections + notes + metrics)."""
    from .handoff import build_handoff_dict, upgrade_handoff

    repo_root = ensure_repo()
    current = upgrade_handoff(build_handoff_dict(repo_root, None))

    previous = None
    if against_path:
        p = pathlib.Path(against_path)
        if not p.exists():
            fail(None, "validation", f"File not found: {against_path}")
        previous = upgrade_handoff(json.loads(p.read_text(encoding="utf-8")))
    else:
        snapshots_dir = repo_root / "Aether-Handoff" / "snapshots"
        if snapshots_dir.is_dir():
            snaps = sorted(snapshots_dir.glob("*.avh"))
            if len(snaps) >= 1:
                try:
                    previous = upgrade_handoff(json.loads(
                        snaps[-1].read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    previous = None

    def _notes(doc):
        return [n.get("note") for n in (doc.get("context_memory") or {}).get("notes", [])]

    def _metrics(doc):
        return doc.get("metrics") or {}

    result: dict = {"against": against_path or "(latest snapshot)"}

    prev_notes, cur_notes = _notes(previous) if previous else [], _notes(current)
    result["notes_added"] = [n for n in cur_notes if n not in prev_notes]
    pm, cm = _metrics(previous) if previous else {}, _metrics(current)
    result["metrics_changed"] = {
        k: {"from": pm.get(k), "to": cm.get(k)}
        for k in set(pm) | set(cm) if pm.get(k) != cm.get(k)
    }
    ss_prev = (previous or {}).get("semantic_summary")
    ss_cur = current.get("semantic_summary")
    result["semantic_summary_changed"] = bool(ss_prev != ss_cur)
    if isinstance(ss_cur, dict):
        result["files_added"] = len((ss_cur.get("files") or {}).get("added", []))
        result["files_removed"] = len((ss_cur.get("files") or {}).get("removed", []))
        result["files_changed"] = len((ss_cur.get("files") or {}).get("changed", []))
    replay_changed = bool(previous and previous.get("replay") != current.get("replay"))
    result["replay_changed"] = replay_changed

    if current_output_mode() == "json":
        emit_json(None, "context diff", data=result)
        return

    click.secho(f"context diff vs {result['against']}:", bold=True)
    for n in result["notes_added"]:
        click.secho(f"  + note: {n}", fg="green")
    for k, ch in result["metrics_changed"].items():
        click.secho(f"  ~ metric {k}: {ch['from']} → {ch['to']}", fg="yellow")
    if isinstance(ss_cur, dict) and (result["files_added"] or result["files_changed"]
                                     or result["files_removed"]):
        click.echo(f"  files: +{result['files_added']} ~{result['files_changed']} "
                   f"-{result['files_removed']}")
    if replay_changed:
        click.secho("  ~ replay recipe changed", fg="yellow")


@context.command()
@click.option("--format", "fmt", type=click.Choice(["avh", "md", "json"]), default="json",
              show_default=True)
@click.option("--out", default=None, help="Write to file instead of stdout.")
def export(fmt: str, out: str | None) -> None:
    """Export the current .avh v2 document (freshly built)."""
    from .handoff import build_handoff_dict, validate_handoff

    repo_root = ensure_repo()
    doc = build_handoff_dict(repo_root, None)
    # v1.3.0 (todo.md item 8): validate on this read path too, not just at write time —
    # a bug in build_handoff_dict() should surface here just as loudly as it would in
    # `av handoff`, rather than silently exporting a document that fails the contract.
    problems = validate_handoff(doc)
    if problems:
        fail(None, "validation",
             "The freshly built .avh document failed validation (this is a bug in "
             "build_handoff_dict, not your input) — " + "; ".join(problems))
    if fmt == "avh":
        rendered = json.dumps(doc, indent=2)
    elif fmt == "json":
        rendered = json.dumps(doc["context_memory"], indent=2)
    else:  # md
        lines = [f"# Aether-Vault Context — {doc.get('current_branch', '?')}"]
        lines.append(f"- commit: `{doc.get('current_commit_hash')}`")
        lin = doc.get("lineage") or {}
        cp = lin.get("code_pointer") or {}
        lines.append(f"- code: `{cp.get('git_remote','?')}@{str(cp.get('git_sha'))[:10]}` dirty={cp.get('dirty')}")
        if doc.get("semantic_summary"):
            lines.append(f"- changes: {doc['semantic_summary'].get('summary')}")
        tail = (doc.get("context_memory") or {}).get("metrics_history_tail") or []
        if tail:
            lines.append("\n## Metric trend\n")
            for e in tail:
                lines.append(f"- `{e['hash']}` {e['message']} → {e['metrics']}")
        notes = (doc.get("context_memory") or {}).get("notes") or []
        if notes:
            lines.append("\n## Agent memory\n")
            for n in notes:
                lines.append(f"- [{(n.get('ts') or '')[:19]}] {n.get('agent','?')}: {n.get('note')}")
        rendered = "\n".join(lines)

    if out:
        pathlib.Path(out).write_text(rendered + ("\n" if not rendered.endswith("\n") else ""),
                                     encoding="utf-8")
        if current_output_mode() == "json":
            emit_json(None, "context export", data={"written": out, "format": fmt})
            return
        click.secho(f"Wrote {out}", fg="green")
        return
    # `--format` (avh/md/json) is this command's OWN content-type flag — independent of
    # the global `--output json` envelope flag. When both are json-shaped, still wrap in
    # the standard envelope (`data.document`) so this command doesn't leak a bare,
    # un-enveloped JSON blob under --output json the way every other command wouldn't.
    if current_output_mode() == "json":
        emit_json(None, "context export", data={"format": fmt, "document": rendered})
        return
    click.echo(rendered)


def ctx_exit(code: int):
    raise SystemExit(code)
