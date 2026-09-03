"""Benchmark #8 — concurrent multi-user push throughput.

Per the resolved methodology question (see BENCHMARKS.md): Aether has a real multi-tenant
FastAPI server (Postgres+Redis-backed) that N clients can push to concurrently against one
shared endpoint. DVC/Git LFS/MLflow have no equivalent concurrent-server primitive — DVC and
Git LFS push to a remote with no app-server tier (concurrency is just filesystem/object-store
writes, not contention on a server process), and MLflow's tracking server maps onto a
different workflow entirely. Rather than approximate three different non-equivalent setups,
v1 scopes this to an Aether-only load test; all three competitor columns are N/A.

Drives `VaultClient.push_commit()` directly (no subprocess) from a thread pool — N concurrent
commits against whichever real `av_server` is reachable, never against a fabricated one.
"""

import concurrent.futures
import hashlib
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from av_cli.client import VaultClient  # noqa: E402

from benchmarks.tool_runner import BenchmarkResult, Row, ToolStatus  # noqa: E402

CONCURRENT_PUSHES = 8


def _fake_commit() -> dict:
    # The server validates `hash` as a real-looking 64-char hex sha256 (`^[a-f0-9]{64}$`,
    # see server.py's push_commit) — uuid4().hex alone is only 32 chars and would 400.
    fake_hash = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    return {
        "hash": fake_hash,
        "parent_hash": None,
        "project_id": f"bench-concurrent-{uuid.uuid4().hex[:8]}",
        "author": "bench",
        "message": "concurrent push bench",
        "timestamp": None,
        "tree": {},
        "tags": [],
        "metrics": {},
    }


def _push_one(_: int) -> bool:
    with VaultClient() as client:
        return client.push_commit(_fake_commit())


def run(tool_order: list[str] | None = None) -> BenchmarkResult:
    tool_order = tool_order or ["av", "git-lfs", "dvc", "mlflow"]
    client = VaultClient()
    server_up = client.server_available()
    client.close()

    values: dict[str, float | None] = {}
    statuses: dict[str, ToolStatus] = {}
    notes: dict[str, str] = {}

    if server_up:
        start = time.perf_counter()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENT_PUSHES) as pool:
                results = list(pool.map(_push_one, range(CONCURRENT_PUSHES)))
            ok = all(results)
        except Exception as exc:
            # A real capture on a resource-constrained machine hit a raw
            # ConnectionResetError under 8-way concurrency (see Probleme.md) — that's a
            # server-was-up-the-whole-time failure, never "not installed". Caught here (not
            # just a False in `results`) because a reset can raise out of push_commit()
            # rather than return False.
            results = []
            ok = False
            notes["av"] = f"server reachable but the operation failed: {exc}"
        elapsed_ms = (time.perf_counter() - start) * 1000
        values["av"] = elapsed_ms if ok else None
        statuses["av"] = ToolStatus.AVAILABLE if ok else ToolStatus.FAILED
        if not ok and "av" not in notes:
            notes["av"] = "server was reachable but one or more of the concurrent pushes failed"
    else:
        values["av"] = None
        statuses["av"] = ToolStatus.NOT_INSTALLED
        notes["av"] = "no av_server reachable to push against"

    for tool in ("git-lfs", "dvc", "mlflow"):
        values[tool] = None
        statuses[tool] = ToolStatus.NOT_APPLICABLE
        notes[tool] = "no comparable concurrent-server primitive (see BENCHMARKS.md methodology)"

    row = Row(
        operation=f"{CONCURRENT_PUSHES} concurrent pushes",
        values=values,
        statuses=statuses,
        unit="ms",
        notes=notes,
    )

    return BenchmarkResult(
        name="concurrent_push",
        title="Concurrent Multi-User Push Throughput",
        description=(
            f"{CONCURRENT_PUSHES} simultaneous commit pushes against a real av_server. "
            "Aether-only for v1 — see methodology note for why the other three are N/A."
        ),
        tool_order=tool_order,
        rows=[row],
    )


if __name__ == "__main__":
    from benchmarks.tool_runner import print_table
    print_table(run())
