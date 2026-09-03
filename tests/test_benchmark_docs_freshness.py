"""v1.3.0: none of README.md's "Benchmark Comparison" summary table, benchmarks/README.md's
numbered list, or development/BENCHMARKS.md's captured tables are mechanically regenerated
from one shared source — each is hand-authored prose over the real numbers `av benchmark`
measures (the per-row narrative, e.g. "unique capability" / "gap widens every commit", isn't
something a template can derive; see benchmarks/README.md's own note). Rather than force a
brittle full auto-generation of hand-written prose, this test is the guard the project's own
established pattern favors instead (same idea as test_docs_commands.py for docs, or
test_ci_policy.py for CI hygiene): it fails loudly, in the same `pytest tests/ -q` every
change already has to pass, the moment either surface goes stale in a way this project has
actually hit for real — a row count drifting out of sync with the real number of benchmarks
(`benchmarks/bench_*.py`, the ground truth `av benchmark` itself iterates), or an unfilled
placeholder like "capture pending" surviving past the run that should have replaced it (see
development/Probleme.md's benchmark-freshness entries — the cold-clone row sat as "capture
pending" in README.md for a full cycle before this test existed).
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STALE_MARKERS = ("capture pending", "tbd", "coming soon", "not yet captured", "todo")


def _real_benchmark_count() -> int:
    return len(list((REPO_ROOT / "benchmarks").glob("bench_*.py")))


def _readme_benchmark_table_rows() -> list[str]:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(
        r"## Benchmark Comparison\n.*?\n\|---\|---\|---\|---\|\n(.*?)\n\n", readme, re.DOTALL
    )
    assert match, "README.md's 'Benchmark Comparison' table not found, or its shape changed"
    return [line for line in match.group(1).splitlines() if line.strip()]


def _benchmarks_md_sections() -> list[str]:
    text = (REPO_ROOT / "development" / "BENCHMARKS.md").read_text(encoding="utf-8")
    skip = {"Reference machine", "Legend", "Methodology notes (resolved open questions)"}
    return [
        h.strip() for h in re.findall(r"^## (.+)$", text, re.MULTILINE)
        if h.strip() not in skip and not h.strip().startswith("Perf history trend")
    ]


def test_readme_benchmark_table_row_count_matches_the_real_benchmark_count():
    rows = _readme_benchmark_table_rows()
    assert len(rows) == _real_benchmark_count(), (
        f"README.md lists {len(rows)} benchmark row(s) but benchmarks/bench_*.py has "
        f"{_real_benchmark_count()} — a benchmark was added/removed on only one side."
    )


def test_readme_benchmark_table_has_no_unfilled_placeholder():
    for row in _readme_benchmark_table_rows():
        lowered = row.lower()
        for marker in STALE_MARKERS:
            assert marker not in lowered, f"README benchmark row still says {marker!r}: {row}"


def test_benchmarks_md_section_count_matches_the_real_benchmark_count():
    sections = _benchmarks_md_sections()
    assert len(sections) == _real_benchmark_count(), (
        f"development/BENCHMARKS.md has {len(sections)} benchmark section(s) but "
        f"benchmarks/bench_*.py has {_real_benchmark_count()} — regenerate via "
        "`av benchmark --markdown development/BENCHMARKS.md`."
    )


def test_benchmarks_readme_numbered_list_matches_the_real_benchmark_count():
    text = (REPO_ROOT / "benchmarks" / "README.md").read_text(encoding="utf-8")
    numbered = re.findall(r"^- `bench_\w+\.py` - #\d+ ", text, re.MULTILINE)
    assert len(numbered) == _real_benchmark_count(), (
        f"benchmarks/README.md's numbered list has {len(numbered)} entries but "
        f"benchmarks/bench_*.py has {_real_benchmark_count()}."
    )
