"""Benchmark #8 — concurrent multi-user push throughput.

Aether has a real multi-tenant FastAPI server that N clients can push to concurrently
against one shared endpoint; DVC/Git LFS/MLflow have no equivalent concurrent-server
primitive, so this is scoped Aether-only (see BENCHMARKS.md methodology) with the other
three columns N/A, and the whole benchmark is excluded from the "faster in every published
domain" claim (`claim_scope="internal"`) rather than silently counted as a pass.

Drives `VaultClient.push_commit()` directly (no subprocess) from a thread pool against
whichever real `av_server` is reachable.
"""

import concurrent.futures
import hashlib
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from av_cli.client import VaultClient  # noqa: E402

from benchmarks.tool_runner import BenchmarkResult, Row, ToolStatus  # noqa: E402

CONCURRENT_PUSHES = 8


def _fake_commit() -> dict:
    # The server validates `hash` as a real-looking 64-char hex sha256 -- uuid4().hex
    # alone is only 32 chars and would 400.
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


def _one_round() -> tuple[float, bool, Exception | None]:
    """One batch of CONCURRENT_PUSHES simultaneous pushes. Returns (elapsed_ms, ok, exc) --
    a reset/failure under concurrency can raise out of push_commit() rather than return
    False, so any exception is captured and reported rather than propagated (a
    server-was-up-the-whole-time failure, never "not installed")."""
    start = time.perf_counter()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENT_PUSHES) as pool:
            results = list(pool.map(_push_one, range(CONCURRENT_PUSHES)))
        return (time.perf_counter() - start) * 1000, all(results), None
    except Exception as exc:  # noqa: BLE001 -- reported to the caller, never silently swallowed
        return (time.perf_counter() - start) * 1000, False, exc


def run(tool_order: list[str] | None = None, repeat: int = 1) -> BenchmarkResult:
    tool_order = tool_order or ["av", "git-lfs", "dvc", "mlflow"]
    client = VaultClient()
    server_up = client.server_available()
    client.close()

    values: dict[str, float | None] = {}
    statuses: dict[str, ToolStatus] = {}
    notes: dict[str, str] = {}

    if server_up:
        rounds = [_one_round() for _ in range(max(1, repeat))]
        ok = all(r[1] for r in rounds)
        elapsed_ms = statistics.median(r[0] for r in rounds)
        first_exc = next((r[2] for r in rounds if r[2] is not None), None)
        values["av"] = elapsed_ms if ok else None
        statuses["av"] = ToolStatus.AVAILABLE if ok else ToolStatus.FAILED
        if not ok:
            notes["av"] = (
                f"server reachable but the operation failed: {first_exc}" if first_exc else
                "server was reachable but one or more of the concurrent pushes failed"
            )
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
        claim_scope="internal",
    )


if __name__ == "__main__":
    from benchmarks.tool_runner import print_table
    print_table(run())
