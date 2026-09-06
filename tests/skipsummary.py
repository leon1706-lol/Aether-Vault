"""Skip-summary aggregation for `pytest_terminal_summary` (wired in tests/conftest.py).

Turns raw skip reasons into an explicit, self-explanatory end-of-run block instead of a
bare "36 skipped" that reads as something being hidden. Pure functions only, no pytest
imports, so classification and rendering are unit-testable in isolation.
"""

from __future__ import annotations

DOCKER_HINT = "docker compose up -d db redis aether-vault-engine"

_DOCKER_MARKERS = ("docker compose", "postgres/redis", "live aether-vault-engine")
_CORE_MARKERS = ("aether_core",)
_PLUGIN_MARKERS = ("lightning", "transformers", "mlflow")


def extract_reason(report) -> str:
    """Best-effort skip-reason text from a pytest TestReport: `pytest.skip(msg)` carries
    longrepr as a 3-tuple ending in `"Skipped: <msg>"`; falls back to longreprtext."""
    lr = getattr(report, "longrepr", None)
    if isinstance(lr, tuple) and len(lr) == 3:
        reason = str(lr[2])
    else:
        text = getattr(report, "longreprtext", "")
        reason = str(text) if text else ""
    if reason.startswith("Skipped: "):
        reason = reason[len("Skipped: "):]
    return reason


def classify_skip(reason: str) -> str:
    """Maps one raw skip message to a bucket: docker-stack, native-core, plugin-extras,
    or other."""
    r = (reason or "").lower()
    if any(marker in r for marker in _DOCKER_MARKERS):
        return "docker-stack"
    if any(marker in r for marker in _CORE_MARKERS):
        return "native-core"
    if any(marker in r for marker in _PLUGIN_MARKERS):
        return "plugin-extras"
    return "other"


def format_skip_note(buckets: dict[str, int]) -> str:
    """Renders the end-of-run block. Empty string when there is nothing to explain."""
    total = sum(buckets.values())
    if total <= 0:
        return ""

    label = "- Skipped by design"
    header = f"{label} {'-' * max(0, 52 - len(label))}"

    parts: list[str] = []
    docker = buckets.get("docker-stack", 0)
    if docker:
        plural = "test needs" if docker == 1 else "tests need"
        parts.append(f"  - {docker} {plural} the Docker registry stack "
                     f"(db/redis/server unreachable)")
        parts.append(f"      -> start it with: {DOCKER_HINT}")
    core = buckets.get("native-core", 0)
    if core:
        parts.append(f"  - {core} optional-dependency guard (native C++ core not built here)")
    plugins = buckets.get("plugin-extras", 0)
    if plugins:
        plural = "guard" if plugins == 1 else "guards"
        parts.append(f"  - {plugins} optional-dependency {plural} (plugin extras)")
    other = buckets.get("other", 0)
    if other:
        parts.append(f"  - {other} other")

    return "\n".join([header, f"  {total} skipped:"] + parts)
