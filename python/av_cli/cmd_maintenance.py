"""Diagnostics & upkeep: doctor/gc/list-meta speed diagnostics.

Bodies moved verbatim from main.py (Point-13 split). Patch-target names owned by
main.py (`_find_source_root`, `_update_readme_test_badge`) are accessed late-bound via
`_root.<name>` so test monkeypatching on the main namespace stays effective.
"""

from .core import *  # noqa: F401,F403 -- shared prelude (stdlib + helpers)
from .core import _get_aether_core, _IGNORED_DIRS
from . import main as _root



@click.command()
def gc() -> None:
    """Trigger garbage collection on the remote CAS server."""
    from .client import VaultClient

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))
    json_mode = current_output_mode() == "json"

    if not client.server_available():
        if json_mode:
            emit_json(None, "gc", data={"ran": False, "reason": "server_unreachable"})
            return
        click.secho("Error: Remote server is not reachable.", fg="red")
        return

    result = client.run_gc()
    if result:
        if json_mode:
            emit_json(None, "gc", data={"ran": True, **result})
            return
        click.secho("[OK] Garbage collection complete", fg="green")
        click.echo(
            f"  Alive objects : {result.get('alive_objects', '?')}\n"
            f"  Deleted objects: {result.get('deleted_objects', '?')}\n"
            f"  Reused trees  : {result.get('reused_trees', '?')}"
        )
    else:
        if json_mode:
            emit_json(None, "gc", data={"ran": False, "reason": "gc_request_failed"})
            return
        click.secho("GC request failed. Check server logs.", fg="red")


def _print_real_repo_speed_diagnostics(repo_root: Path) -> None:
    """`av doctor --speed` — read-only timing snapshot of the current repo."""
    probes = speedcheck.run_real_repo_probes(repo_root, load_config, iter_working_files)
    click.echo("")
    click.secho("=== Speed diagnostics (this repo) ===", bold=True, fg="cyan")
    click.echo(f"{'Probe':<42} {'Time':>10}")
    click.echo("-" * 53)
    for label, elapsed_ms in probes:
        click.echo(f"{label:<42} {elapsed_ms:>8.1f} ms")


def _compose_env_dict(svc: dict) -> dict:
    """docker-compose `environment:` is either a mapping or a list of `KEY=VALUE` strings
    — normalizes both to a plain dict. A bare `KEY` (no `=`) list entry means "pass through
    the host's value" and has no value to migrate, so it's skipped rather than raising."""
    env = svc.get("environment") or {}
    if isinstance(env, dict):
        return {k: v for k, v in env.items()}
    return dict(e.split("=", 1) for e in env if "=" in e)


def _doctor_compose_migrate(path_str: str, write: bool) -> None:
    """v1.3.0 (todo.md item 20): rewrites a pinned legacy two-container compose file
    (separate aether-vault-server / aether-vault-webui services, the pre-v1.2.2 split
    topology) into the consolidated one-container AV_ENGINE_ROLE=all form the project has
    shipped since. Detects the legacy shape narrowly — an image referencing
    'aether-vault-server'/'aether-vault-webui', or the DATABASE_URL/NEXT_PUBLIC_API_URL
    env markers engine-entrypoint.sh's own legacy auto-detect already keys off of — rather
    than attempting a general-purpose compose rewrite; fails cleanly with a clear message
    when a file doesn't match instead of guessing at an unfamiliar shape. See
    docs/migrate-engine-image.md."""
    try:
        import yaml
    except ImportError:
        fail(None, "validation",
             "av doctor --compose requires PyYAML (`pip install PyYAML`), which isn't "
             "installed. This tool only touches a compose file you point it at — it's "
             "not needed for anything else `av` does.")

    path = Path(path_str)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        fail(None, "validation", f"{path}: not valid YAML ({exc}).")
    services = doc.get("services") or {}
    if not isinstance(services, dict):
        fail(None, "validation", f"{path}: no top-level 'services:' mapping found.")

    def _is_server(svc: dict) -> bool:
        return "aether-vault-server" in str(svc.get("image", "")) \
            or "DATABASE_URL" in _compose_env_dict(svc)

    def _is_webui(svc: dict) -> bool:
        return "aether-vault-webui" in str(svc.get("image", "")) \
            or "NEXT_PUBLIC_API_URL" in _compose_env_dict(svc)

    server_name = next((n for n, s in services.items() if isinstance(s, dict) and _is_server(s)), None)
    webui_name = next((n for n, s in services.items() if isinstance(s, dict) and _is_webui(s)), None)
    if not server_name or not webui_name:
        fail(None, "validation",
             f"{path}: could not find both a legacy server AND webui service (looked for "
             "an 'aether-vault-server'/'aether-vault-webui' image, or DATABASE_URL/"
             "NEXT_PUBLIC_API_URL in each service's environment) — nothing to migrate, or "
             "this file doesn't match the expected pre-v1.2.2 two-container shape.")

    server_svc, webui_svc = services[server_name], services[webui_name]
    merged_env = {**_compose_env_dict(webui_svc), **_compose_env_dict(server_svc)}
    merged_env["AV_ENGINE_ROLE"] = "all"

    engine_service: dict = {
        "image": "ghcr.io/leon1706-lol/aether-vault-engine:latest",
        "ports": ["8000:8000", "3000:3000"],
        "environment": merged_env,
        "restart": server_svc.get("restart") or webui_svc.get("restart") or "unless-stopped",
    }
    # v1.2.5: the compose files this project ships set a 30s stop_grace_period so
    # AV_ENGINE_STOP_GRACE_SECS' drain window isn't cut short by Docker's 10s default —
    # carry that forward (or set it) on the migrated service, not just copy silently past it.
    engine_service["stop_grace_period"] = (
        server_svc.get("stop_grace_period") or webui_svc.get("stop_grace_period") or "30s"
    )
    depends_on = server_svc.get("depends_on") or webui_svc.get("depends_on")
    if depends_on:
        engine_service["depends_on"] = depends_on
    volumes = server_svc.get("volumes")
    if volumes:
        engine_service["volumes"] = volumes

    new_services = {k: v for k, v in services.items() if k not in (server_name, webui_name)}
    new_services["aether-vault-engine"] = engine_service
    new_doc = {**doc, "services": new_services}
    rendered = yaml.safe_dump(new_doc, sort_keys=False, default_flow_style=False)

    if current_output_mode() == "json":
        emit_json(None, "doctor", data={
            "mode": "compose_migrate", "path": str(path), "applied": write,
            "removed_services": [server_name, webui_name],
            "added_service": "aether-vault-engine", "rendered": rendered,
        })
    else:
        if write:
            click.secho(
                f"Rewriting {path}: merging '{server_name}' + '{webui_name}' into "
                "'aether-vault-engine'.", fg="green")
        else:
            click.secho(
                f"[DRY RUN] Would merge '{server_name}' + '{webui_name}' into "
                f"'aether-vault-engine' in {path}:", fg="cyan")
            click.echo(rendered)
            click.secho("Pass --write to apply this rewrite in place.", fg="yellow")
    if write:
        atomic_write_text(path, rendered)


@click.command()
@click.option("--fix", is_flag=True, default=False,
              help="Repair fixable issues: re-link pointers, clear tmp leftovers, clear/retry pending-push entries.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="With --fix, preview what would be repaired without changing anything.")
@click.option("--speed", "speed", is_flag=True, default=False,
              help="Also print a read-only timing snapshot of this repo's hot paths (index load, config load, file scan, storage stats).")
@click.option("--compose", "compose_path", type=click.Path(exists=True, dir_okay=False), default=None,
              help="v1.3.0: instead of diagnosing a repo, rewrite a pinned legacy "
                   "two-container (aether-vault-server + aether-vault-webui) compose file "
                   "at PATH into the consolidated one-container AV_ENGINE_ROLE=all form. "
                   "Dry-run by default (prints the rewrite); pass --write to apply it. "
                   "See docs/migrate-engine-image.md.")
@click.option("--write", "write_compose", is_flag=True, default=False,
              help="With --compose, apply the rewrite in place instead of only previewing it.")
def doctor(fix: bool, dry_run: bool, speed: bool, compose_path: str | None, write_compose: bool) -> None:
    """Diagnose common repo and environment problems.

    Read-only by default: reports issues (native core availability, server reachability,
    index/pointer consistency, pending-push queue, leftover temp files) without modifying
    anything. Pass --fix to repair what's safely recoverable, or --fix --dry-run to preview
    what --fix would do without changing anything.

    `--compose PATH` is a completely different mode (a compose-file migration tool, not a
    repo diagnostic) — see that option's help.
    """
    if compose_path:
        _doctor_compose_migrate(compose_path, write_compose)
        return

    from .client import VaultClient

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))
    av_dir = repo_root / ".av"

    json_mode = current_output_mode() == "json"
    checks: list[dict] = []  # v1.3.0: structured mirror of every ok/warn/fixed call,
                              # emitted as one envelope in JSON mode instead of each
                              # check's own click.secho line — see emit_json call at the
                              # bottom of this command.

    if not json_mode:
        click.secho("Aether-Vault Doctor", bold=True)
        click.secho("-------------------", bold=True)

    warning_count = 0
    fixed_count = 0
    preview = fix and dry_run  # --dry-run only means anything alongside --fix

    def ok(msg: str) -> None:
        checks.append({"level": "ok", "message": msg})
        if not json_mode:
            click.secho(f"[OK]    {msg}", fg="green")

    def warn(msg: str) -> None:
        nonlocal warning_count
        warning_count += 1
        checks.append({"level": "warn", "message": msg})
        if not json_mode:
            click.secho(f"[WARN]  {msg}", fg="yellow")

    def fixed(msg: str) -> None:
        nonlocal fixed_count
        fixed_count += 1
        checks.append({"level": "would_fix" if preview else "fixed", "message": msg})
        if not json_mode:
            label = "[WOULD FIX]" if preview else "[FIXED]"
            click.secho(f"{label} {msg}", fg="cyan")

    required = [av_dir / "objects", av_dir / "refs" / "heads", av_dir / "commits", av_dir / "HEAD"]
    missing = [str(p.relative_to(repo_root)) for p in required if not p.exists()]
    if missing:
        warn(f"Missing repository structure: {', '.join(missing)}")
    else:
        ok(f"Repository found at {av_dir}")

    if _get_aether_core():
        ok("Native core (aether_core) loaded — hashing runs at full speed")
    else:
        warn("Native core (aether_core) not loaded — falling back to slower Python hashing")

    if client.server_available():
        ok(f"Remote server {cfg.get('remote_url')} reachable")
    else:
        warn(f"Remote server {cfg.get('remote_url')} unreachable — commits will queue locally")

    idx = Index(repo_root)
    ok(f"Index (.av/index) is valid JSON, {len(idx.entries)} entries")

    # --- Orphaned pointer entries: index entry has a pointer but its CAS object is missing ---
    # An entry with a pointer but no matching CAS object means `av checkout` would silently
    # fail to materialize that file's real content. Split artifacts don't store a whole-file
    # blob at all (see add()'s comment) — for those, "missing content" means a missing
    # *layer* (safetensors) or *chunk* (.pt/.pth/.ckpt CDC), not the absent-by-design
    # whole-file object.
    def _missing_parts(entry: dict, key: str) -> list[dict]:
        return [
            part for part in entry.get(key) or []
            if not (av_dir / "objects" / part["hash"][:2] / part["hash"][2:]).exists()
        ]

    def _artifact_content_missing(entry: dict) -> bool:
        if entry.get("layers"):
            return bool(_missing_parts(entry, "layers"))
        if entry.get("chunks"):
            return bool(_missing_parts(entry, "chunks"))
        return not (av_dir / "objects" / entry["hash"][:2] / entry["hash"][2:]).exists()

    orphaned = [
        rel_path
        for rel_path, entry in idx.entries.items()
        if entry.get("pointer") and _artifact_content_missing(entry)
    ]
    recovered_orphans = []
    unrecovered_orphans = []
    for rel_path in orphaned:
        entry = idx.entries[rel_path]
        can_recover = False
        parts_key = "layers" if entry.get("layers") else ("chunks" if entry.get("chunks") else None)
        if parts_key:
            missing = _missing_parts(entry, parts_key)
            if fix and preview:
                can_recover = all(client.object_exists(p["hash"]) for p in missing)
            elif fix:
                can_recover = client.server_available() and all(
                    client.download_object(p["hash"], av_dir / "objects" / p["hash"][:2] / p["hash"][2:])
                    for p in missing
                )
        else:
            h = entry["hash"]
            obj_path = av_dir / "objects" / h[:2] / h[2:]
            if fix and preview:
                can_recover = client.object_exists(h)
            elif fix:
                can_recover = client.server_available() and client.download_object(h, obj_path)
        (recovered_orphans if can_recover else unrecovered_orphans).append(rel_path)

    if recovered_orphans:
        verb = "Would re-link" if preview else "Re-linked"
        for rel_path in recovered_orphans:
            fixed(f"{verb} {rel_path} by downloading its object from the remote")
    if unrecovered_orphans:
        note = " (--fix could not recover: not available locally or on the remote)" if fix else ""
        warn(f"{len(unrecovered_orphans)} pointer entry(ies) missing their object: {', '.join(unrecovered_orphans)}{note}")
    elif not orphaned:
        pointer_count = sum(1 for e in idx.entries.values() if e.get("pointer"))
        ok(f"No orphaned pointer entries ({pointer_count} pointer(s), all matched)")

    # --- Stale .av-pointer files: pointer file on disk with no corresponding tracked entry ---
    # Left behind by a manual delete/rename of the original file outside of `av`.
    stale_pointers = []  # list of (pointer_rel_path, original_rel_path, pointer_abs_path)
    for dirpath, dirnames, files in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for f in files:
            if not f.endswith(".av-pointer"):
                continue
            ptr_path = Path(dirpath) / f
            original_rel = str(ptr_path.relative_to(repo_root))[: -len(".av-pointer")].replace("\\", "/")
            if original_rel not in idx.entries:
                ptr_rel = str(ptr_path.relative_to(repo_root)).replace("\\", "/")
                stale_pointers.append((ptr_rel, original_rel, ptr_path))

    recovered_stale = []
    unrecovered_stale = []
    for ptr_rel, original_rel, ptr_path in stale_pointers:
        try:
            parsed = parse_pointer(ptr_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            parsed = None
        if not parsed:
            note = " (--fix could not parse this pointer file)" if fix else ""
            unrecovered_stale.append(f"{ptr_rel}{note}")
            continue

        h, size = parsed["hash"], parsed["size"]
        obj_path = av_dir / "objects" / h[:2] / h[2:]
        object_available = obj_path.exists()

        if not object_available and fix:
            if preview:
                object_available = client.object_exists(h)
            elif client.server_available() and client.download_object(h, obj_path):
                object_available = True

        if object_available and fix:
            if not preview:
                idx.add_entry(original_rel, h, size, 0, "artifact", ptr_rel)
            recovered_stale.append(ptr_rel)
        elif object_available:
            unrecovered_stale.append(ptr_rel)
        else:
            note = " (--fix could not recover: object missing locally and on the remote)" if fix else ""
            unrecovered_stale.append(f"{ptr_rel}{note}")

    if recovered_stale:
        verb = "Would re-link" if preview else "Re-linked"
        for ptr_rel in recovered_stale:
            fixed(f"{verb} {ptr_rel} back into the index")
    if unrecovered_stale:
        warn(f"{len(unrecovered_stale)} stale .av-pointer file(s) with no tracked entry: {', '.join(unrecovered_stale)}")

    # --- Pending-push queue ---
    pending = load_pending_push(repo_root)
    if not fix:
        if pending:
            warn(f"{len(pending)} commit(s) pending push (.av/pending_push) — run `av push` once the server is back")
        else:
            ok("No commits pending push")
    else:
        commits_dir = av_dir / "commits"
        recoverable_pending = []
        unrecoverable_pending = []
        for entry in pending:
            if (commits_dir / f"{entry['commit_hash']}.json").exists():
                recoverable_pending.append(entry)
            else:
                unrecoverable_pending.append(entry)

        if unrecoverable_pending:
            verb = "Would clear" if preview else "Cleared"
            fixed(f"{verb} {len(unrecoverable_pending)} unrecoverable pending-push entry(ies) (commit object missing locally)")
            if not preview:
                save_pending_push(repo_root, recoverable_pending)

        if recoverable_pending:
            if preview:
                if client.server_available():
                    fixed(f"Would retry pushing {len(recoverable_pending)} remaining pending commit(s) (server reachable)")
                else:
                    warn(f"{len(recoverable_pending)} commit(s) pending push (.av/pending_push) — run `av push` once the server is back")
            elif client.server_available():
                still_pending = flush_pending_push(repo_root, client)
                pushed = len(recoverable_pending) - len(still_pending)
                if pushed:
                    fixed(f"Retried pending push queue: pushed {pushed}, {len(still_pending)} still pending")
                if still_pending:
                    warn(f"{len(still_pending)} commit(s) pending push (.av/pending_push) — run `av push` once the server is back")
            else:
                warn(f"{len(recoverable_pending)} commit(s) pending push (.av/pending_push) — run `av push` once the server is back")
        elif not pending:
            ok("No commits pending push")

    # --- *.tmp.* leftovers from an interrupted atomic write ---
    tmp_leftovers = list(av_dir.rglob("*.tmp.*"))
    if tmp_leftovers:
        names = [str(p.relative_to(repo_root)) for p in tmp_leftovers]
        if fix:
            verb = "Would remove" if preview else "Removed"
            fixed(f"{verb} {len(tmp_leftovers)} leftover temp file(s): {', '.join(names)}")
            if not preview:
                for p in tmp_leftovers:
                    p.unlink(missing_ok=True)
        else:
            warn(f"{len(tmp_leftovers)} leftover temp file(s) from an interrupted write: {', '.join(names)}")
    else:
        ok("No *.tmp.* leftover files in .av/")

    speed_probes = None
    if speed:
        if json_mode:
            speed_probes = [{"label": label, "elapsed_ms": elapsed_ms} for label, elapsed_ms in
                            speedcheck.run_real_repo_probes(repo_root, load_config, iter_working_files)]
        else:
            _print_real_repo_speed_diagnostics(repo_root)

    if json_mode:
        emit_json(None, "doctor", data={
            "checks": checks, "warning_count": warning_count, "fixed_count": fixed_count,
            "dry_run": preview, "speed_probes": speed_probes,
        })
        return

    click.echo("")
    suffix = " (dry run — nothing was changed)" if preview else ""
    if fixed_count and warning_count:
        verb = "would be fixed" if preview else "fixed"
        click.secho(f"{fixed_count} issue(s) {verb}, {warning_count} warning(s) remain, 0 errors.{suffix}", fg="cyan", bold=True)
    elif fixed_count:
        verb = "would be fixed" if preview else "fixed"
        click.secho(f"{fixed_count} issue(s) {verb}, 0 warnings remain, 0 errors.{suffix}", fg="cyan", bold=True)
    elif warning_count:
        click.secho(f"{warning_count} warning(s), 0 errors.{suffix}", fg="yellow", bold=True)
    else:
        click.secho(f"Everything looks good.{suffix}", fg="green", bold=True)
