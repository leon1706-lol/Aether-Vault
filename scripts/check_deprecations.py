"""Reads `development/deprecations.yml` and reports which deprecated surfaces are still
pending, overdue for removal, or confirmed removed. Read-only — a human decides what to
actually remove.

Usage:
  python scripts/check_deprecations.py --dry-run
      Prints every entry's status, always exits 0 (the nightly job's own mode).
  python scripts/check_deprecations.py --current-version 1.4.0
      Same report, plus exits 1 if any "pending" entry's remove_in is <= the given version.
"""
import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPRECATIONS_PATH = REPO_ROOT / "development" / "deprecations.yml"

_VALID_STATUSES = {"pending", "removed"}
_REQUIRED_KEYS = {"surface", "announced_in", "remove_in", "status"}


def load_entries(path: Path = DEPRECATIONS_PATH) -> list[dict]:
    import yaml

    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("deprecations", []) or []


def validate_entry(entry: dict) -> list[str]:
    """Returns a list of problems (empty = valid). Pure, no I/O — the schema half of
    tests/test_deprecations.py calls this directly."""
    problems = []
    missing = _REQUIRED_KEYS - entry.keys()
    if missing:
        problems.append(f"missing required key(s): {sorted(missing)}")
        return problems  # can't check anything else meaningfully without these
    if entry["status"] not in _VALID_STATUSES:
        problems.append(f"status {entry['status']!r} not one of {sorted(_VALID_STATUSES)}")
    if entry["status"] == "removed" and entry.get("probe") is not None:
        problems.append("status is 'removed' but 'probe' is set (should be null — nothing left to probe)")
    if entry["status"] == "pending" and not entry.get("notes"):
        problems.append("status is 'pending' but has no 'notes' explaining the migration path")
    return problems


def _parse_version(v: str) -> tuple:
    return tuple(int(p) for p in v.lstrip("v").split(".") if p.isdigit())


def is_overdue(entry: dict, current_version: str) -> bool:
    if entry["status"] != "pending":
        return False
    return _parse_version(current_version) >= _parse_version(entry["remove_in"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only, always exit 0 (the nightly job's own mode)")
    parser.add_argument("--current-version", default=None,
                        help="Check pending entries against this version; exits 1 if any are overdue")
    args = parser.parse_args()

    entries = load_entries()
    if not entries:
        print("No deprecation entries recorded.")
        return 0

    pending = [e for e in entries if e.get("status") == "pending"]
    removed = [e for e in entries if e.get("status") == "removed"]

    print(f"{len(entries)} deprecation entrie(s): {len(pending)} pending, {len(removed)} removed\n")

    overdue = []
    for entry in pending:
        note = ""
        if args.current_version and is_overdue(entry, args.current_version):
            note = "  <-- OVERDUE for removal"
            overdue.append(entry)
        print(f"[pending] {entry['surface']} (announced {entry['announced_in']}, remove in {entry['remove_in']}){note}")

    for entry in removed:
        print(f"[removed] {entry['surface']} (announced {entry['announced_in']}, removed in {entry['remove_in']})")

    if args.dry_run or not args.current_version:
        return 0

    if overdue:
        print(f"\n{len(overdue)} entrie(s) are OVERDUE for removal at version {args.current_version}.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
