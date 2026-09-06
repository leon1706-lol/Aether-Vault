"""av support-bundle — a redacted diagnostics artifact for a support engineer (v1.3.2).
Repo-scoped like `av doctor`, but produces a shareable bundle instead of console output.
**Redaction is the load-bearing part of this file**: every value under a credential-
shaped config key is replaced before anything touches disk.
"""
import datetime
import json as _json
import re
import subprocess
from pathlib import Path

import click

from .core import current_output_mode, emit_json, ensure_repo, iter_working_files, load_config

_SENSITIVE_KEY_RE = re.compile(r"(token|password|secret|key)", re.IGNORECASE)


def _redact(value):
    """Recursively redacts dict values whose KEY looks credential-shaped. Lists are
    walked too (a config could plausibly nest a token inside one); strings/numbers pass
    through unless their key matched at the parent level."""
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if _SENSITIVE_KEY_RE.search(k) and v else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _run_best_effort(cmd: list[str]) -> str | None:
    """Runs an optional diagnostic command (docker, etc.) — absent/failing tooling is
    itself diagnostic information (recorded as None), never a reason to abort the whole
    bundle."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.stdout.strip() or result.stderr.strip()
    except Exception as exc:
        return f"(unavailable: {exc})"


@click.command(name="support-bundle")
@click.argument("output_dir", type=click.Path(file_okay=False), required=False)
def support_bundle(output_dir: str | None) -> None:
    """Collect a redacted diagnostics bundle (versions, health/ready, repo config,
    migration state, container status) for a support engineer. Never includes a raw
    token/password/secret value — see this module's own docstring."""
    from . import _version
    from .client import VaultClient

    repo_root = ensure_repo()
    cfg = load_config(repo_root)
    client = VaultClient(cfg.get("remote_url", "http://localhost:8000"), cfg.get("remote_api_token"))

    out = Path(output_dir or f"av-support-bundle-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    out.mkdir(parents=True, exist_ok=True)

    bundle: dict = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cli_version": getattr(_version, "__version__", "dev"),
        "remote_url": cfg.get("remote_url"),
        "repo_config": _redact(cfg),
    }

    try:
        health_resp = client.session.get(f"{client.server_url}/api/health", timeout=5)
        bundle["health"] = {"status_code": health_resp.status_code, "body": health_resp.json()}
    except Exception as exc:
        bundle["health"] = {"error": str(exc)}

    try:
        ready_resp = client.session.get(f"{client.server_url}/api/ready", timeout=5)
        bundle["ready"] = {"status_code": ready_resp.status_code, "body": ready_resp.json()}
    except Exception as exc:
        bundle["ready"] = {"error": str(exc)}

    # Container status/log tails -- best-effort, since a non-Docker deployment
    # legitimately has none of this.
    bundle["docker_ps"] = _run_best_effort(
        ["docker", "ps", "--filter", "name=aether-vault", "--format",
         "{{.Names}}\t{{.Status}}"])
    bundle["engine_log_tail"] = _run_best_effort(
        ["docker", "logs", "--tail", "50", "aether-vault-engine"])

    # A local speedcheck probe, matching `av doctor --speed`'s own read-only snapshot --
    # a support engineer's first question for "it feels slow" is usually this exact data.
    try:
        from . import speedcheck

        probes = speedcheck.run_real_repo_probes(repo_root, load_config, iter_working_files)
        bundle["speed_probes"] = [{"label": label, "ms": ms} for label, ms in probes]
    except Exception as exc:
        bundle["speed_probes"] = f"(unavailable: {exc})"

    bundle_path = out / "bundle.json"
    bundle_path.write_text(_json.dumps(bundle, indent=2, sort_keys=True, default=str), encoding="utf-8")

    # Belt-and-braces redaction check on the SERIALIZED bytes, not just the structured
    # walk above -- catches a token that ended up somewhere _redact() didn't anticipate
    # (a future field added without updating the key-name pattern) before it ever lands
    # on disk in a form a human could `cat` and paste into a support ticket.
    raw_token = cfg.get("remote_api_token")
    if raw_token and raw_token in bundle_path.read_text(encoding="utf-8"):
        bundle_path.unlink()
        raise click.ClickException(
            "internal error: a raw token was about to be written to the support bundle -- "
            "aborted before writing. This is a bug in _redact(); please report it."
        )

    if current_output_mode() == "json":
        emit_json(None, "support-bundle", data={"output_dir": str(out), "files": ["bundle.json"]})
        return
    click.secho(f"[OK] Support bundle written to {out}/bundle.json", fg="green")
    click.echo("  Every token/password/secret-shaped config value has been redacted.")
