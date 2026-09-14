#!/usr/bin/env python
"""RSS scoreboard: peak/avg resident memory of every Aether-Vault process class, measured
with the real `av` CLI in a scratch repo outside the checkout (never inside it).

    python scripts/rss_scoreboard.py --out-json development/memory-baseline-v1.6.2.json \
        --out-md /tmp/baseline.md --label v1.6.2-baseline [--with-docker] \
        [--pytest-target tests/test_server.py]

Rows (each is `--runs` invocations; peak = max of per-run peaks, avg = mean of them):
  status_cold, add_small_files, add_safetensors (one 256 MiB file),
  add_many_safetensors (8 x 64 MiB in one add: the multi-worker peak),
  commit, log_all                                                     -- AV_NO_DAEMON=1
  status_daemon_client, status_daemon, daemon_after_add,
  daemon_idle_trimmed                                                 -- a foreground daemon
  engine_idle, postgres_idle, redis_idle, server_idle, webui_idle      -- --with-docker only
  pytest_<file>                                                       -- --pytest-target

Output JSON schema "rss-scoreboard-1.0" is what `scripts/append_perf_history.py
--from-scoreboard` and `development/MEMORY.md`'s before/after table consume.
Dependency-free: `av_cli.sysres` does the measuring (no psutil).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "python"))

from av_cli import sysres  # noqa: E402

SCHEMA = "rss-scoreboard-1.0"
ALL_SCENARIOS = [
    "status_cold", "add_small_files", "add_safetensors", "add_many_safetensors", "commit", "log_all",
    "status_daemon_client", "status_daemon", "daemon_after_add", "daemon_idle_trimmed",
    "engine_idle", "postgres_idle", "redis_idle", "server_idle", "webui_idle",
]
DAEMON_SCENARIOS = {"status_daemon_client", "status_daemon", "daemon_after_add", "daemon_idle_trimmed"}
DOCKER_SCENARIOS = {"engine_idle", "postgres_idle", "redis_idle", "server_idle", "webui_idle"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def write_synthetic_safetensors(path: Path, n_layers: int = 8, layer_bytes: int = 32 * 1024 * 1024,
                                seed: int = 1234) -> None:
    """A structurally valid safetensors file: 8-byte LE header length, JSON header with
    dtype/shape/data_offsets per tensor, then the payload. Payload is seeded
    pseudo-random (incompressible, unique per layer) so dedup never short-circuits the
    staging path being measured."""
    header = {}
    offset = 0
    for i in range(n_layers):
        header[f"layer_{i}.weight"] = {
            "dtype": "F16",
            "shape": [layer_bytes // 2 // 1024, 1024],
            "data_offsets": [offset, offset + layer_bytes],
        }
        offset += layer_bytes
    header["__metadata__"] = {"format": "pt", "generator": "rss_scoreboard"}
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    pad = (8 - len(header_bytes) % 8) % 8
    header_bytes += b" " * pad
    import random

    rng = random.Random(seed)
    block = 1024 * 1024
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(header_bytes)))
        f.write(header_bytes)
        for i in range(n_layers):
            rng.seed(seed + i)
            remaining = layer_bytes
            while remaining > 0:
                chunk = min(block, remaining)
                f.write(rng.randbytes(chunk))
                remaining -= chunk


def populate_small_files(root: Path, count: int) -> None:
    src = root / "src"
    src.mkdir(exist_ok=True)
    for i in range(count):
        d = src / f"pkg{i % 20}"
        d.mkdir(exist_ok=True)
        (d / f"mod{i}.py").write_text(f"# file {i}\nVALUE = {i}\n" * 4, encoding="utf-8")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class Scoreboard:
    def __init__(self, av: str, runs: int, files: int, safetensors_mib: int, commits: int,
                 echo=print) -> None:
        self.av = av
        self.runs = runs
        self.files = files
        self.safetensors_mib = safetensors_mib
        self.commits = commits
        self.echo = echo
        self.rows: dict[str, dict] = {}
        self.root = Path(tempfile.mkdtemp(prefix="av-rss-"))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.model_src = self.root / "model.safetensors"

    # -- helpers ----------------------------------------------------------------

    def env(self, **extra: str) -> dict[str, str]:
        env = dict(os.environ)
        env["AV_NO_DAEMON"] = "1"
        env.pop("AV_DAEMON", None)
        env["NO_COLOR"] = "1"
        env.update(extra)
        return env

    def av_cmd(self, *args: str) -> list[str]:
        return [self.av, *args]

    def run_av(self, *args: str, env: dict | None = None, check: bool = True) -> sysres.MeasuredRun:
        run = sysres.run_measured(self.av_cmd(*args), cwd=str(self.repo), env=env or self.env(),
                                  capture_output=True, timeout=1800)
        if check and run.returncode != 0:
            raise RuntimeError(f"av {' '.join(args)} failed ({run.returncode}):\n{run.stdout}\n{run.stderr}")
        return run

    def record(self, name: str, runs: list[sysres.MeasuredRun] | None = None, *, peak: float | None = None,
               source: str = "n/a", elapsed_ms: float | None = None, count: int = 0) -> None:
        if runs:
            peaks = [r.peak_rss_mb for r in runs if r.peak_rss_mb is not None]
            self.rows[name] = {
                "peak_rss_mb": round(max(peaks), 1) if peaks else None,
                "avg_peak_rss_mb": round(statistics.mean(peaks), 1) if peaks else None,
                "elapsed_ms": round(statistics.median(r.elapsed_ms for r in runs), 1),
                "runs": len(runs),
                "source": runs[0].source if runs else "n/a",
            }
        else:
            self.rows[name] = {
                "peak_rss_mb": round(peak, 1) if peak is not None else None,
                "avg_peak_rss_mb": round(peak, 1) if peak is not None else None,
                "elapsed_ms": round(elapsed_ms, 1) if elapsed_ms is not None else None,
                "runs": count,
                "source": source,
            }
        row = self.rows[name]
        self.echo(f"  {name:<24} peak={row['peak_rss_mb']} MB  avg={row['avg_peak_rss_mb']} MB  "
                  f"elapsed={row['elapsed_ms']} ms  [{row['source']}]")

    # -- setup ------------------------------------------------------------------

    def setup(self, need_model: bool) -> None:
        self.echo(f"scratch repo: {self.repo}")
        self.run_av("init", "--mode", "local", "--yes", "--no-repl")
        populate_small_files(self.repo, self.files)
        if need_model:
            self.echo(f"writing synthetic safetensors ({self.safetensors_mib} MiB)...")
            n_layers = max(1, self.safetensors_mib // 32)
            write_synthetic_safetensors(self.model_src, n_layers=n_layers,
                                        layer_bytes=self.safetensors_mib * 1024 * 1024 // n_layers)

    def fresh_model_copy(self, name: str = "model.safetensors") -> Path:
        dest = self.repo / name
        if dest.exists():
            dest.unlink()
        shutil.copyfile(self.model_src, dest)
        return dest

    # -- scenarios --------------------------------------------------------------

    def scenario_status_cold(self) -> None:
        self.run_av("add", ".")
        self.record("status_cold", [self.run_av("status") for _ in range(self.runs)])

    def scenario_add_small_files(self) -> None:
        runs = []
        for i in range(self.runs):
            (self.repo / "src" / f"touch_{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
            for p in (self.repo / "src").glob("pkg0/*.py"):
                p.write_text(p.read_text(encoding="utf-8") + f"# rev {i}\n", encoding="utf-8")
            runs.append(self.run_av("add", "."))
        self.record("add_small_files", runs)

    def scenario_add_safetensors(self) -> None:
        runs = []
        for i in range(self.runs):
            name = f"model_{i}.safetensors"
            self.fresh_model_copy(name)
            # Rewrite one byte in the first layer so each run stages fresh content.
            with open(self.repo / name, "r+b") as f:
                f.seek(4096 + i)
                f.write(b"\xff")
            runs.append(self.run_av("add", name))
        self.record("add_safetensors", runs)

    def scenario_add_many_safetensors(self, count: int = 8, mib_each: int = 64) -> None:
        """`count` distinct safetensors staged in ONE `av add` -- the multi-worker peak
        (each worker holds its own layer buffer), which a single file can never show."""
        runs = []
        for i in range(self.runs):
            many = self.repo / f"many_{i}"
            many.mkdir()
            for j in range(count):
                n_layers = max(1, mib_each // 32)
                write_synthetic_safetensors(many / f"shard_{j}.safetensors", n_layers=n_layers,
                                            layer_bytes=mib_each * 1024 * 1024 // n_layers, seed=1000 * i + j)
            runs.append(self.run_av("add", str(many.relative_to(self.repo))))
        self.record("add_many_safetensors", runs)

    def scenario_commit(self) -> None:
        runs = []
        for i in range(self.runs):
            (self.repo / "src" / f"commit_{i}.py").write_text(f"y = {i}\n", encoding="utf-8")
            self.run_av("add", ".")
            runs.append(self.run_av("commit", "-m", f"scoreboard {i}", "--no-upload"))
        self.record("commit", runs)

    def scenario_log_all(self) -> None:
        self.echo(f"  creating {self.commits} commits for log_all...")
        for i in range(self.commits):
            (self.repo / "src" / "log_churn.py").write_text(f"z = {i}\n", encoding="utf-8")
            self.run_av("add", "src/log_churn.py")
            self.run_av("commit", "-m", f"churn {i}", "--no-upload")
        self.record("log_all", [self.run_av("log", "--all", "--limit", "30") for _ in range(self.runs)])

    def scenario_daemon(self, wanted: set[str]) -> None:
        env = dict(os.environ)
        env.pop("AV_NO_DAEMON", None)
        env["AV_DAEMON"] = "0"  # use-only: never auto-spawn a second daemon behind our back
        env["AV_DAEMON_TRIM_SECS"] = "3"
        env["NO_COLOR"] = "1"
        proc = subprocess.Popen(self.av_cmd("daemon", "start", "--foreground"), cwd=str(self.repo), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # `av daemon start` on PATH is the launcher; the daemon proper is its child, so the
        # tree is what we want. The `av status` clients spawned below are children of this
        # script, never of the daemon, so they can't leak into this sampler.
        sampler = sysres.ChildRssSampler(proc, include_descendants=True)
        sampler.start()
        try:
            for _ in range(100):
                st = subprocess.run(self.av_cmd("--output", "json", "daemon", "status"), cwd=str(self.repo),
                                    env=env, capture_output=True, text=True, timeout=60)
                if st.returncode == 0 and '"running": true' in st.stdout:
                    break
                if proc.poll() is not None:
                    raise RuntimeError("daemon exited before becoming ready (see Probleme.md #144)")
                time.sleep(0.2)
            else:
                raise RuntimeError("daemon never reported running")

            client_runs = [sysres.run_measured(self.av_cmd("status"), cwd=str(self.repo), env=env,
                                               capture_output=True, timeout=300) for _ in range(5)]
            if "status_daemon_client" in wanted:
                self.record("status_daemon_client", client_runs)
            if "status_daemon" in wanted:
                self.record("status_daemon", peak=_current_child_rss(proc.pid, sampler), source=sampler.source,
                            count=5)
            if "daemon_after_add" in wanted:
                self.fresh_model_copy("model_daemon.safetensors")
                r = sysres.run_measured(self.av_cmd("add", "model_daemon.safetensors"), cwd=str(self.repo),
                                        env=env, capture_output=True, timeout=1800)
                if r.returncode != 0:
                    raise RuntimeError(f"daemon add failed: {r.stdout}\n{r.stderr}")
                self.record("daemon_after_add", peak=_current_child_rss(proc.pid, sampler), source=sampler.source,
                            elapsed_ms=r.elapsed_ms, count=1)
            if "daemon_idle_trimmed" in wanted:
                time.sleep(8)
                st = subprocess.run(self.av_cmd("--output", "json", "daemon", "status"), cwd=str(self.repo),
                                    env=env, capture_output=True, text=True, timeout=60)
                trimmed_rss = None
                try:
                    trimmed_rss = json.loads(st.stdout)["data"].get("rss_mb")
                except Exception:
                    pass
                self.record("daemon_idle_trimmed", peak=trimmed_rss if trimmed_rss is not None
                            else _current_child_rss(proc.pid, sampler), source="daemon-status" if trimmed_rss
                            else sampler.source, count=1)
        finally:
            subprocess.run(self.av_cmd("daemon", "stop"), cwd=str(self.repo), env=env,
                           capture_output=True, timeout=60)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            sampler.stop()

    def scenario_docker(self, wanted: set[str]) -> None:
        stats = docker_stats()
        if stats is None:
            for name in wanted:
                self.record(name, peak=None, source="n/a")
            return
        mapping = {"engine_idle": "aether-vault-engine", "postgres_idle": "aether-vault-db",
                   "redis_idle": "aether-vault-redis"}
        for name, container in mapping.items():
            if name in wanted:
                self.record(name, peak=stats.get(container), source="docker-stats" if container in stats else "n/a")
        if wanted & {"server_idle", "webui_idle"}:
            procs = docker_exec_ps("aether-vault-engine")
            if "server_idle" in wanted:
                self.record("server_idle", peak=procs.get("python"), source="docker-ps" if procs else "n/a")
            if "webui_idle" in wanted:
                self.record("webui_idle", peak=procs.get("node"), source="docker-ps" if procs else "n/a")

    def scenario_pytest(self, target: str, extra: list[str] | None = None) -> None:
        name = "pytest_" + Path(target).stem
        run = sysres.run_measured(
            [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider", "--no-cov", "-o", "addopts=",
             *(extra or [])],
            cwd=str(REPO_ROOT), env=self.env(), capture_output=True, timeout=3600)
        self.record(name, [run])
        self.rows[name]["returncode"] = run.returncode
        tail = (run.stdout or "").strip().splitlines()
        self.rows[name]["summary"] = tail[-1] if tail else ""

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _current_child_rss(pid: int, sampler: sysres.ChildRssSampler) -> float | None:
    """The daemon's resident set right now (not its peak) -- the steady-state number."""
    sampler.sample()
    return sampler.current_mb


# ---------------------------------------------------------------------------
# Docker probes
# ---------------------------------------------------------------------------

def parse_mem_usage(text: str) -> float | None:
    """`docker stats` MemUsage column ("123.4MiB / 7.6GiB") → MiB of the first number."""
    head = text.split("/")[0].strip()
    units = {"B": 1 / (1024 * 1024), "KiB": 1 / 1024, "kB": 1 / 1024, "MiB": 1.0, "MB": 1.0,
             "GiB": 1024.0, "GB": 1024.0}
    for unit in sorted(units, key=len, reverse=True):
        if head.endswith(unit):
            try:
                return round(float(head[: -len(unit)]) * units[unit], 1)
            except ValueError:
                return None
    return None


def docker_stats() -> dict[str, float] | None:
    if shutil.which("docker") is None:
        return None
    try:
        result = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out: dict[str, float] = {}
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        name, mem = line.split("\t", 1)
        value = parse_mem_usage(mem)
        if value is not None:
            out[name.strip()] = value
    return out


def docker_exec_ps(container: str) -> dict[str, float]:
    """Largest RSS per command name inside a container (python = uvicorn, node = Next)."""
    try:
        result = subprocess.run(["docker", "exec", container, "ps", "-eo", "rss=,comm="],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    out: dict[str, float] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            rss_mb = int(parts[0]) / 1024
        except ValueError:
            continue
        comm = parts[1].strip()
        # Next.js's standalone server renames itself "next-server (vX.Y.Z)".
        if comm.startswith("python"):
            key = "python"
        elif comm.startswith(("node", "next-server")):
            key = "node"
        else:
            key = comm
        out[key] = max(out.get(key, 0.0), round(rss_mb, 1))
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_markdown(doc: dict) -> str:
    lines = [
        f"# RSS scoreboard — {doc['label']}",
        "",
        f"Captured {doc['captured']} at `{doc['git_sha']}` on {doc['machine']['os']} / Python "
        f"{doc['machine']['python']} / {doc['machine']['total_ram_mb']} MB RAM.",
        "",
        "| Scenario | Peak RSS (MB) | Avg | Elapsed | Source |",
        "|---|---:|---:|---:|---|",
    ]
    for name, row in doc["scenarios"].items():
        peak = "n/a" if row["peak_rss_mb"] is None else f"{row['peak_rss_mb']:.1f}"
        avg = "n/a" if row["avg_peak_rss_mb"] is None else f"{row['avg_peak_rss_mb']:.1f}"
        elapsed = "n/a" if row["elapsed_ms"] is None else f"{row['elapsed_ms']:.0f} ms"
        lines.append(f"| {name} | {peak} | {avg} | {elapsed} | {row['source']} |")
    return "\n".join(lines) + "\n"


def git_sha() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                           text=True, timeout=10)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def build_document(label: str, rows: dict) -> dict:
    return {
        "schema": SCHEMA,
        "captured": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "label": label,
        "git_sha": git_sha(),
        "machine": {
            "os": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "total_ram_mb": sysres.total_ram_mb(),
        },
        "scenarios": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-md")
    ap.add_argument("--label", default="scoreboard")
    ap.add_argument("--scenarios", default=",".join(s for s in ALL_SCENARIOS if s not in DOCKER_SCENARIOS),
                    help="comma-separated subset (default: every non-docker row)")
    ap.add_argument("--files", type=int, default=2000)
    ap.add_argument("--safetensors-mib", type=int, default=256)
    ap.add_argument("--commits", type=int, default=200)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--with-docker", action="store_true")
    ap.add_argument("--pytest-target", action="append", default=[])
    ap.add_argument("--pytest-extra", default="",
                    help='extra pytest args for every --pytest-target, e.g. \'-k "gc or audit"\'')
    ap.add_argument("--av", default=shutil.which("av") or "av")
    ap.add_argument("--keep", action="store_true", help="keep the scratch repo for inspection")
    args = ap.parse_args(argv)

    wanted = {s.strip() for s in args.scenarios.split(",") if s.strip()}
    unknown = wanted - set(ALL_SCENARIOS)
    if unknown:
        ap.error(f"unknown scenarios: {sorted(unknown)}")
    if args.with_docker:
        wanted |= DOCKER_SCENARIOS

    board = Scoreboard(args.av, args.runs, args.files, args.safetensors_mib, args.commits)
    need_model = bool(wanted & {"add_safetensors", "daemon_after_add"})
    try:
        board.setup(need_model=need_model)
        if "status_cold" in wanted:
            board.scenario_status_cold()
        if "add_small_files" in wanted:
            board.scenario_add_small_files()
        if "add_safetensors" in wanted:
            board.scenario_add_safetensors()
        if "add_many_safetensors" in wanted:
            board.scenario_add_many_safetensors()
        if "commit" in wanted:
            board.scenario_commit()
        if "log_all" in wanted:
            board.scenario_log_all()
        if wanted & DAEMON_SCENARIOS:
            board.scenario_daemon(wanted & DAEMON_SCENARIOS)
        if wanted & DOCKER_SCENARIOS:
            board.scenario_docker(wanted & DOCKER_SCENARIOS)
        import shlex

        for target in args.pytest_target:
            board.scenario_pytest(target, shlex.split(args.pytest_extra))
    finally:
        if not args.keep:
            board.cleanup()
        else:
            print(f"kept scratch repo at {board.root}")

    doc = build_document(args.label, board.rows)
    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_json).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    if args.out_md:
        Path(args.out_md).write_text(render_markdown(doc), encoding="utf-8")
    print(f"wrote {args.out_json}" + (f" and {args.out_md}" if args.out_md else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
