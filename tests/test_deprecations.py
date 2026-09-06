"""v1.3.4 (todo.md item 18): schema validation for development/deprecations.yml, plus an
overdue guard against THIS repo's own real current version — same factoring rationale as
test_release_gate.py (logic lives in scripts/check_deprecations.py, unit-tested here
without needing a real release to exercise it).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_deprecations.py"
_spec = importlib.util.spec_from_file_location("check_deprecations", _SCRIPT_PATH)
cd = importlib.util.module_from_spec(_spec)
sys.modules["check_deprecations"] = cd
_spec.loader.exec_module(cd)


# ---------------------------------------------------------------------------
# validate_entry (pure schema check)
# ---------------------------------------------------------------------------

def test_valid_removed_entry_has_no_problems():
    entry = {"surface": "x", "announced_in": "v1.0.0", "remove_in": "v1.1.0",
             "status": "removed", "probe": None}
    assert cd.validate_entry(entry) == []


def test_valid_pending_entry_needs_notes():
    entry = {"surface": "x", "announced_in": "v1.0.0", "remove_in": "v2.0.0",
             "status": "pending", "notes": "migrate via av doctor"}
    assert cd.validate_entry(entry) == []


def test_missing_required_key_is_flagged():
    entry = {"surface": "x", "announced_in": "v1.0.0", "status": "removed"}
    problems = cd.validate_entry(entry)
    assert problems and "remove_in" in problems[0]


def test_invalid_status_is_flagged():
    entry = {"surface": "x", "announced_in": "v1.0.0", "remove_in": "v1.1.0", "status": "gone"}
    problems = cd.validate_entry(entry)
    assert any("status" in p for p in problems)


def test_removed_entry_with_a_probe_set_is_flagged():
    entry = {"surface": "x", "announced_in": "v1.0.0", "remove_in": "v1.1.0",
             "status": "removed", "probe": "some check"}
    problems = cd.validate_entry(entry)
    assert any("probe" in p for p in problems)


def test_pending_entry_with_no_notes_is_flagged():
    entry = {"surface": "x", "announced_in": "v1.0.0", "remove_in": "v2.0.0", "status": "pending"}
    problems = cd.validate_entry(entry)
    assert any("notes" in p for p in problems)


# ---------------------------------------------------------------------------
# is_overdue
# ---------------------------------------------------------------------------

def test_is_overdue_true_when_current_version_reached_remove_in():
    entry = {"status": "pending", "remove_in": "v1.4.0"}
    assert cd.is_overdue(entry, "1.4.0") is True
    assert cd.is_overdue(entry, "1.5.0") is True


def test_is_overdue_false_before_remove_in():
    entry = {"status": "pending", "remove_in": "v1.4.0"}
    assert cd.is_overdue(entry, "1.3.4") is False


def test_is_overdue_false_for_a_removed_entry_regardless_of_version():
    entry = {"status": "removed", "remove_in": "v1.0.0"}
    assert cd.is_overdue(entry, "9.9.9") is False


# ---------------------------------------------------------------------------
# The REAL registry (development/deprecations.yml) — schema + overdue, for real
# ---------------------------------------------------------------------------

def _real_entries():
    return cd.load_entries()


def test_real_registry_has_at_least_one_entry():
    assert _real_entries(), "development/deprecations.yml has no entries at all"


@pytest.mark.parametrize("entry", _real_entries(), ids=lambda e: e.get("surface", "?")[:60])
def test_real_entry_is_schema_valid(entry):
    problems = cd.validate_entry(entry)
    assert not problems, f"{entry.get('surface')!r}: {problems}"


def test_no_real_pending_entry_is_overdue_against_this_repos_own_version():
    # This repo's own current version (from git tags via setuptools-scm, same source
    # release_gate.py's checks already trust) -- a "pending" entry whose remove_in this
    # repo has already reached but never actually removed is a real, actionable finding,
    # not a false positive.
    from python.av_cli import __version__

    bare_version = __version__.split("+")[0].split(".dev")[0]
    overdue = [e for e in _real_entries() if cd.is_overdue(e, bare_version)]
    assert not overdue, (
        f"deprecation(s) overdue for removal at version {bare_version}: "
        f"{[e['surface'] for e in overdue]} — either remove the surface and flip "
        f"status to 'removed', or this repo's MAJOR-only removal policy "
        f"(VERSIONING.md) means remove_in should be a later version"
    )
