"""v1.3.0: makes the "no dependency bots, no auto-merge, anywhere in the CI path" rule
permanent instead of a convention someone re-litigates later (explicit owner instruction
for the v1.3.0 depth pass). Stack-free, so it runs in the main `test` job on every push —
a bot config or an auto-merge step re-entering `.github/` fails CI immediately, by name.

Dependabot was deliberately removed in Phase 55 (development/CHANGELOG.md) by owner
decision — "dependency review is manual now" (development/infrastructure.md). This test
is what keeps that decision from silently drifting.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GITHUB_DIR = REPO_ROOT / ".github"
WORKFLOWS_DIR = GITHUB_DIR / "workflows"

# Config files that would re-introduce a dependency bot.
_FORBIDDEN_BOT_CONFIGS = ["dependabot.yml", "dependabot.yaml", "renovate.json",
                          "renovate.json5", ".renovaterc", ".renovaterc.json"]

# Substrings that, if found anywhere in a workflow file, indicate a dependency bot,
# auto-merge, or an unreviewed-PR-privilege trigger — case-insensitive.
_FORBIDDEN_SUBSTRINGS = [
    "dependabot", "renovate", "pull_request_target",
    "gh pr merge", "--auto", "mergify",
    "peter-evans/create-pull-request",
]

# A workflow step that pushes commits back to the repo — the mechanism an auto-updating
# bot would use even without matching any of the substrings above.
_PUSH_BACK_PATTERNS = [
    re.compile(r"git\s+push\b"),
    re.compile(r"ad-m/github-push-action"),
    re.compile(r"stefanzweifel/git-auto-commit-action"),
]


def _all_workflow_files():
    if not WORKFLOWS_DIR.is_dir():
        return []
    return sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))


class TestNoDependencyBotConfig:
    @pytest.mark.parametrize("name", _FORBIDDEN_BOT_CONFIGS)
    def test_bot_config_file_does_not_exist(self, name):
        assert not (GITHUB_DIR / name).exists(), (
            f".github/{name} exists — dependency bots were deliberately removed "
            f"(Phase 55, development/CHANGELOG.md); reintroducing one needs an explicit, "
            f"separate owner decision, not a silent config drop."
        )


class TestNoBotOrAutoMergeInWorkflows:
    @pytest.mark.parametrize("path", _all_workflow_files(), ids=lambda p: p.name)
    def test_workflow_has_no_forbidden_substring(self, path):
        text = path.read_text(encoding="utf-8").lower()
        hits = [s for s in _FORBIDDEN_SUBSTRINGS if s in text]
        assert not hits, (
            f"{path.relative_to(REPO_ROOT)} contains forbidden substring(s) {hits} — "
            f"this repo's CI must never open, approve, or merge a PR on its own; every "
            f"check may only block a publish, per explicit owner instruction."
        )

    @pytest.mark.parametrize("path", _all_workflow_files(), ids=lambda p: p.name)
    def test_workflow_never_pushes_commits_back_to_the_repo(self, path):
        text = path.read_text(encoding="utf-8")
        hits = [p.pattern for p in _PUSH_BACK_PATTERNS if p.search(text)]
        assert not hits, (
            f"{path.relative_to(REPO_ROOT)} appears to push commits back to the repo "
            f"({hits}) — no workflow in this repo may do that; a release step that "
            f"commits generated data (e.g. perf-history.json) must be run locally by "
            f"the maintainer, not automated in CI."
        )


def test_at_least_one_workflow_file_exists():
    # A guard against this whole test module silently asserting nothing (an empty
    # parametrize list passes trivially) if .github/workflows/ ever went missing.
    assert _all_workflow_files(), "no workflow files found under .github/workflows/"
