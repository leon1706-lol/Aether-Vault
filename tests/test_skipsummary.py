"""Unit tests for the skip-summary aggregation (tests/skipsummary.py).

The classification/formatting pair is what every `pytest` run's tail now prints — these
tests pin the bucketing against the exact reason strings the suite actually produces
(see the skip census in tests/test_server.py's header and the importorskip sites).
"""

from types import SimpleNamespace

from skipsummary import classify_skip, extract_reason, format_skip_note


def _report(reason: str):
    """Mimics pytest's skipped-report longrepr shape: (path, lineno, 'Skipped: <msg>')."""
    return SimpleNamespace(longrepr=("some/path.py", 12, f"Skipped: {reason}"))


def test_classify_docker_stack_variants():
    assert classify_skip(
        "Live aether-vault-server not reachable on :8000; run "
        "`docker compose up -d db redis aether-vault-server`"
    ) == "docker-stack"
    assert classify_skip(
        "Postgres/Redis test services not reachable "
        "(AV_TEST_DATABASE_URL=postgresql://..., AV_TEST_REDIS_URL=...). "
        "Run `docker compose up -d db redis` first."
    ) == "docker-stack"


def test_classify_native_core_importorskip():
    # pytest.importorskip("aether_core") default message shape
    assert classify_skip("could not import 'aether_core': No module named 'aether_core'") \
        == "native-core"


def test_classify_plugin_extras():
    assert classify_skip("lightning not installed") == "plugin-extras"
    assert classify_skip("transformers is installed; ImportError path not exercised") \
        == "plugin-extras"
    assert classify_skip("mlflow not installed") == "plugin-extras"


def test_classify_other_falls_through():
    assert classify_skip("some entirely new reason") == "other"
    assert classify_skip("") == "other"


def test_extract_reason_strips_prefix_from_tuple_longrepr():
    assert extract_reason(_report("lightning not installed")) == "lightning not installed"


def test_extract_reason_survives_odd_shapes():
    assert extract_reason(SimpleNamespace(longrepr=None, longreprtext="fallback text")) \
        == "fallback text"
    assert extract_reason(SimpleNamespace()) == ""


def test_format_note_empty_when_no_skips():
    assert format_skip_note({}) == ""
    assert format_skip_note({"docker-stack": 0}) == ""


def test_format_note_full_block_matches_approved_shape():
    note = format_skip_note({"docker-stack": 33, "native-core": 1, "plugin-extras": 2})
    assert "- Skipped by design" in note
    assert "36 skipped:" in note
    assert "33 tests need the Docker registry stack (db/redis/server unreachable)" in note
    assert "start it with: docker compose up -d db redis aether-vault-server" in note
    assert "1 optional-dependency guard (native C++ core not built here)" in note
    assert "2 optional-dependency guards (plugin extras)" in note
    # docker hint sits directly under its own line, before the other buckets
    assert note.index("33 tests need") < note.index("-> start it with") < \
        note.index("2 optional-dependency guards")
    # ASCII-only so Windows consoles (cp1252/cp850) can't trigger escape fallbacks
    assert all(ord(ch) < 128 for ch in note)


def test_format_note_singular_grammar():
    note = format_skip_note({"docker-stack": 1})
    assert "1 test needs the Docker registry stack" in note

    note = format_skip_note({"plugin-extras": 1})
    assert "1 optional-dependency guard (plugin extras)" in note
