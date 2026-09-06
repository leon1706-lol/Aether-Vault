"""v1.3.4 (todo.md item 32): keeps `development/infrastructure.md`'s "## CI Job Map"
table and `.github/ci-budgets.yml` from silently drifting away from the REAL job ids in
`.github/workflows/*.yml` — a hand-maintained doc/config can say anything; this parses
all three and asserts they agree, in both directions (a new job with no doc row, or a
doc row for a job that no longer exists, both fail).

Also checks two structural properties every job/workflow should have going forward: a
`timeout-minutes`, and a top-level `permissions:` block on its workflow (both v1.3.4
hardening — see AGENTS.md/infrastructure.md's own notes on why).
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
INFRASTRUCTURE_MD = REPO_ROOT / "development" / "infrastructure.md"
CI_BUDGETS = REPO_ROOT / ".github" / "ci-budgets.yml"

_WORKFLOW_FILES = ["tests.yml", "security.yml", "codeql.yml", "nightly.yml",
                   "release.yml", "docker-edge.yml"]


def _load_workflow(name: str) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))


def _real_jobs(name: str) -> set[str]:
    return set((_load_workflow(name) or {}).get("jobs", {}) or {})


def _ci_job_map_section() -> str:
    text = INFRASTRUCTURE_MD.read_text(encoding="utf-8")
    match = re.search(r"^## CI Job Map\n(.*?)(?=\n## )", text, re.MULTILINE | re.DOTALL)
    assert match, "development/infrastructure.md has no '## CI Job Map' section"
    return match.group(1)


# A workflow's own mini-table is introduced by a line like "**`tests.yml`** — ..." —
# everything up to the NEXT such bold-filename line (or end of section) belongs to it.
_WORKFLOW_HEADER_RE = re.compile(r"^\*\*`(" + "|".join(re.escape(w) for w in _WORKFLOW_FILES) + r")`\*\*", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\|(?!---)(.+)\|\s*$", re.MULTILINE)
_FIRST_BACKTICK_RE = re.compile(r"`([a-zA-Z][\w-]*)`")


def _documented_jobs() -> dict[str, set[str]]:
    section = _ci_job_map_section()
    headers = list(_WORKFLOW_HEADER_RE.finditer(section))
    assert headers, "CI Job Map section has no '**<workflow>.yml**' subsection headers"

    result: dict[str, set[str]] = {w: set() for w in _WORKFLOW_FILES}
    for i, header in enumerate(headers):
        workflow = header.group(1)
        start = header.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(section)
        chunk = section[start:end]
        for row in _TABLE_ROW_RE.finditer(chunk):
            # A markdown table cell can contain a literal `\|` (this table's own
            # codeql.yml row does, inside a backtick span) -- an ESCAPED pipe, not a
            # cell separator. Protect it before splitting, restore after.
            protected = row.group(1).replace("\\|", "\x00")
            cells = [c.replace("\x00", "|") for c in protected.split("|")]
            if len(cells) < 2 or cells[0].strip() in ("Surface", "---"):
                continue
            job_cell = cells[-1]
            match = _FIRST_BACKTICK_RE.search(job_cell)
            if match:
                result[workflow].add(match.group(1))
    return result


@pytest.mark.parametrize("workflow", _WORKFLOW_FILES)
class TestCiJobMapMatchesRealWorkflows:
    def test_every_real_job_is_documented(self, workflow):
        real = _real_jobs(workflow)
        documented = _documented_jobs()[workflow]
        missing = real - documented
        assert not missing, (
            f"{workflow} has job(s) with no row in infrastructure.md's CI Job Map: "
            f"{sorted(missing)} — add one (or extend an existing row) under the "
            f"'**{workflow}**' subsection"
        )

    def test_every_documented_job_still_exists(self, workflow):
        real = _real_jobs(workflow)
        documented = _documented_jobs()[workflow]
        stale = documented - real
        assert not stale, (
            f"infrastructure.md's CI Job Map documents job(s) under '**{workflow}**' "
            f"that no longer exist in the workflow: {sorted(stale)} — the job was "
            f"renamed or removed without updating this table"
        )


@pytest.mark.parametrize("workflow", _WORKFLOW_FILES)
class TestEveryJobHasATimeoutAndPermissions:
    def test_every_job_has_timeout_minutes(self, workflow):
        jobs = (_load_workflow(workflow) or {}).get("jobs", {}) or {}
        missing = [job_id for job_id, job in jobs.items() if "timeout-minutes" not in job]
        assert not missing, f"{workflow} has job(s) with no timeout-minutes: {missing}"

    def test_workflow_has_top_level_permissions(self, workflow):
        data = _load_workflow(workflow) or {}
        assert "permissions" in data, (
            f"{workflow} has no top-level `permissions:` block — every workflow in "
            f"this repo should declare a least-privilege default (v1.3.4, W1b)"
        )


def _budgets_for(workflow: str) -> dict:
    yaml = pytest.importorskip("yaml")
    if not CI_BUDGETS.exists():
        return {}
    data = yaml.safe_load(CI_BUDGETS.read_text(encoding="utf-8")) or {}
    return (data.get("workflows", {}) or {}).get(workflow, {}) or {}


@pytest.mark.parametrize("workflow", _WORKFLOW_FILES)
class TestCiBudgetsMatchRealJobs:
    def test_every_real_job_has_a_budget_entry(self, workflow):
        real = _real_jobs(workflow)
        budgeted = set(_budgets_for(workflow))
        missing = real - budgeted
        assert not missing, (
            f".github/ci-budgets.yml's '{workflow}' section is missing budget(s) for: "
            f"{sorted(missing)}"
        )

    def test_every_budget_entry_still_names_a_real_job(self, workflow):
        real = _real_jobs(workflow)
        budgeted = set(_budgets_for(workflow))
        stale = budgeted - real
        assert not stale, (
            f".github/ci-budgets.yml's '{workflow}' section has stale entry(ies) for "
            f"job(s) that no longer exist: {sorted(stale)}"
        )
