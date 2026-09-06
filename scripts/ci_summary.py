"""v1.3.4 (todo.md item 29/31, W5d): CI summary dashboard — job name, conclusion,
duration, budget, and overrun delta for the current workflow run, rendered as a Markdown
table for `$GITHUB_STEP_SUMMARY` and (on same-repo PRs) posted as a PR comment. Never
gating: this only ever reads `GET /repos/{repo}/actions/runs/{run_id}/jobs` and
`.github/ci-budgets.yml`, and always exits 0 — a slow job gets an `::warning::`
annotation, not a failed build (see this file's own module docstring on why: the same
"a hard gate on noise trains reviewers to ignore it" judgment call security.yml's own
scanners already document for this repo).

Usage: python scripts/ci_summary.py --workflow tests.yml --run-id 12345 [--repo owner/name]
Prints the Markdown table to stdout; the caller decides where that goes
($GITHUB_STEP_SUMMARY, `gh pr comment`, or just a terminal for local debugging).
"""
import argparse
import datetime
import json
import sys
from pathlib import Path


def load_budgets(repo_root: Path, workflow: str) -> dict[str, int]:
    """Reads `.github/ci-budgets.yml`'s section for one workflow file. Returns {} (not
    an error) when the file or that workflow's section is missing — a job with no budget
    entry just never gets an overrun warning, which `tests/test_ci_map.py` is what
    actually enforces completeness of this file, not this function."""
    import yaml

    path = repo_root / ".github" / "ci-budgets.yml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return (data.get("workflows", {}) or {}).get(workflow, {}) or {}


def _parse_ts(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def job_duration_minutes(job: dict) -> float | None:
    started = _parse_ts(job.get("started_at"))
    completed = _parse_ts(job.get("completed_at"))
    if started is None or completed is None:
        return None
    return (completed - started).total_seconds() / 60.0


def render_summary(jobs: list[dict], budgets: dict[str, int]) -> tuple[str, list[str]]:
    """Returns (markdown_table, overrun_warning_lines). A matrix job's check-run name
    (e.g. "test (3.14)") is matched against a budget by its BASE job id (the part before
    the first " (") falling back to an exact match — so one budget entry covers every
    matrix leg of that job."""
    lines = ["| Job | Conclusion | Duration | Budget | Delta |", "|---|---|---|---|---|"]
    warnings = []
    for job in jobs:
        name = job.get("name", "?")
        conclusion = job.get("conclusion") or job.get("status", "?")
        duration = job_duration_minutes(job)
        base_id = name.split(" (")[0]
        budget = budgets.get(name, budgets.get(base_id))

        duration_str = f"{duration:.1f}m" if duration is not None else "—"
        budget_str = f"{budget}m" if budget is not None else "—"
        delta_str = "—"
        emoji = "✅" if conclusion == "success" else ("⏭️" if conclusion == "skipped" else "❌")

        if duration is not None and budget is not None:
            delta = duration - budget
            if delta > 0:
                delta_str = f"+{delta:.1f}m over"
                warnings.append(
                    f"{name} took {duration:.1f}m, over its {budget}m budget by {delta:.1f}m"
                )
            else:
                delta_str = f"{delta:.1f}m under"

        lines.append(f"| {name} | {emoji} {conclusion} | {duration_str} | {budget_str} | {delta_str} |")

    return "\n".join(lines), warnings


def fetch_run_jobs(repo: str, run_id: str, gh_token: str | None) -> list[dict]:
    import urllib.error
    import urllib.request

    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if gh_token:
        req.add_header("Authorization", f"Bearer {gh_token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("jobs", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", required=True, help="Workflow file name, e.g. tests.yml")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repo", default="leon1706-lol/aether-vault")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--gh-token", default=None)
    args = parser.parse_args()

    import os
    gh_token = args.gh_token or os.environ.get("GITHUB_TOKEN")

    budgets = load_budgets(args.repo_root, args.workflow)
    jobs = fetch_run_jobs(args.repo, args.run_id, gh_token)
    table, warnings = render_summary(jobs, budgets)

    print(f"### CI summary — {args.workflow} run {args.run_id}\n")
    print(table)
    if warnings:
        print(f"\n**{len(warnings)} job(s) over budget:**")
        for w in warnings:
            print(f"- {w}")
        for w in warnings:
            print(f"::warning::{w}", file=sys.stderr)
    else:
        print("\nAll jobs within budget (or unbudgeted — see `.github/ci-budgets.yml`).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
