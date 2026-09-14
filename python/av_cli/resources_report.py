"""`av doctor --resources`: the live memory picture of this machine and this repo's
daemon, plus the low-memory knobs worth setting on it (V1.6.3). Pure data -- the doctor
command renders it; nothing here prints."""
from __future__ import annotations

from pathlib import Path

#: Below this much total RAM (or free RAM) the "lowmem" profile is recommended: the
#: reference dev box (3.9 GB, Docker's vmmem taking ~500 MB) is the case this was built for.
LOWMEM_TOTAL_MB = 6144
LOWMEM_AVAILABLE_MB = 1024


def build_resources_report(repo_root: Path | None) -> dict:
    from . import core, sysres
    from ._version import __version__

    snap = sysres.snapshot()
    daemon: dict = {"running": False, "rss_mb": None, "peak_rss_mb": None}
    if repo_root is not None:
        try:
            from . import daemon_client

            status = daemon_client.read_status(repo_root, __version__)
        except Exception:
            status = None
        if status:
            daemon = {
                "running": bool(status.get("running", True)),
                "rss_mb": status.get("rss_mb"),
                "peak_rss_mb": status.get("peak_rss_mb"),
            }

    threads = core.resolve_threads(repo_root)
    stage_workers = core.effective_stage_workers(threads, snap["available_mb"])
    stage_buffer_mb = core._stage_buffer_cap_bytes() // (1024 * 1024)
    per_worker_mb = core.stage_worker_budget_mb()

    total = snap["total_ram_mb"] or 0.0
    available = snap["available_mb"] or 0.0
    lowmem = (total and total < LOWMEM_TOTAL_MB) or (available and available < LOWMEM_AVAILABLE_MB)
    profile = "lowmem" if lowmem else "default"

    recommendations: list[str] = []
    if lowmem:
        recommendations.append(
            f"set AV_STAGE_WORKERS_MAX=2 (staging currently runs up to {stage_workers} workers x "
            f"~{per_worker_mb} MB each)")
        if stage_buffer_mb > 8:
            recommendations.append(
                f"set AV_STAGE_BUFFER_MB=8 (layers > 8 MiB stream through disk instead of a "
                f"{stage_buffer_mb} MiB RAM buffer)")
        recommendations.append("use the low-memory .env block for the registry stack (README: Low-memory mode)")
        if daemon["running"]:
            recommendations.append("set AV_DAEMON_TRIM_SECS=5 so the daemon trims sooner after going idle, "
                                   "or AV_NO_DAEMON=1 to keep no resident process at all")
    import sys

    if sys.platform == "win32" and lowmem:
        recommendations.append("bound Docker Desktop's WSL VM in %USERPROFILE%\\.wslconfig "
                               "([wsl2] memory=1200MB) -- that VM, not the containers, is what eats the RAM")

    return {
        "rss_mb": snap["rss_mb"],
        "peak_rss_mb": snap["peak_rss_mb"],
        "total_ram_mb": snap["total_ram_mb"],
        "available_mb": snap["available_mb"],
        "daemon": daemon,
        "stage_workers_effective": stage_workers,
        "stage_buffer_mb": stage_buffer_mb,
        "stage_worker_budget_mb": per_worker_mb,
        "stage_peak_estimate_mb": stage_workers * per_worker_mb + 30,
        "recommended_profile": profile,
        "recommendations": recommendations,
    }


def render_resources_report(report: dict, echo) -> None:
    def mb(v):
        return "n/a" if v is None else f"{v:,.0f} MB"

    echo("")
    echo("Resources")
    echo("---------")
    echo(f"  this process : {mb(report['rss_mb'])} RSS (peak {mb(report['peak_rss_mb'])})")
    echo(f"  machine      : {mb(report['available_mb'])} free of {mb(report['total_ram_mb'])}")
    d = report["daemon"]
    if d["running"]:
        echo(f"  daemon       : running, {mb(d['rss_mb'])} RSS (peak {mb(d['peak_rss_mb'])})")
    else:
        echo("  daemon       : not running")
    echo(f"  staging      : up to {report['stage_workers_effective']} worker(s) x ~{report['stage_worker_budget_mb']} MB "
         f"(AV_STAGE_BUFFER_MB={report['stage_buffer_mb']}) -> est. peak ~{report['stage_peak_estimate_mb']} MB")
    echo(f"  profile      : {report['recommended_profile']}")
    for rec in report["recommendations"]:
        echo(f"  - {rec}")
