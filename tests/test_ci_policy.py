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


# Every third-party `uses:` reference must be pinned to a full commit SHA with a
# trailing `# vX.Y` comment, not a mutable tag or branch ref.
_ACTION_USES_RE = re.compile(r"^(\s*(?:-\s*)?uses:\s*)([^\s#]+)(.*)$", re.MULTILINE)
# A local composite action (`uses: ./.github/actions/x`) or a Docker image reference
# (`docker://...`) are exempt from the owner/repo@sha40 shape below.
_LOCAL_OR_DOCKER_ACTION_RE = re.compile(r"^\./|^docker://")
_SHA_PINNED_ACTION_RE = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w.-]+)*@[0-9a-f]{40}$")


def _iter_uses_lines(text: str):
    for match in _ACTION_USES_RE.finditer(text):
        ref = match.group(2)
        trailing_comment = match.group(3)
        yield ref, trailing_comment


class TestActionPinning:
    @pytest.mark.parametrize("path", _all_workflow_files(), ids=lambda p: p.name)
    def test_every_action_reference_is_sha_pinned(self, path):
        text = path.read_text(encoding="utf-8")
        offenders = []
        for ref, _comment in _iter_uses_lines(text):
            if _LOCAL_OR_DOCKER_ACTION_RE.match(ref):
                continue
            if not _SHA_PINNED_ACTION_RE.match(ref):
                offenders.append(ref)
        assert not offenders, (
            f"{path.relative_to(REPO_ROOT)} has non-SHA-pinned action reference(s): "
            f"{offenders} — every `uses:` must be `owner/repo@<40-hex-sha>`, never a "
            f"mutable tag (@v5) or branch (@release/v1); see the pinned entries in this "
            f"same file for the `# vX.Y` trailing-comment convention"
        )

    @pytest.mark.parametrize("path", _all_workflow_files(), ids=lambda p: p.name)
    def test_every_sha_pinned_action_names_its_version_in_a_comment(self, path):
        text = path.read_text(encoding="utf-8")
        unlabeled = []
        for ref, comment in _iter_uses_lines(text):
            if _LOCAL_OR_DOCKER_ACTION_RE.match(ref) or not _SHA_PINNED_ACTION_RE.match(ref):
                continue
            if "# v" not in comment and "#v" not in comment:
                unlabeled.append(ref)
        assert not unlabeled, (
            f"{path.relative_to(REPO_ROOT)} has SHA-pinned action(s) with no trailing "
            f"`# vX.Y` comment naming the human-readable version: {unlabeled} — a bare "
            f"40-hex SHA is unreadable in review; every pin must say which release it is"
        )

    @pytest.mark.parametrize("path", _all_workflow_files(), ids=lambda p: p.name)
    def test_no_container_image_is_unpinned_latest(self, path):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("image:"):
                continue
            image_ref = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            assert "@sha256:" in image_ref or re.search(r":[0-9]", image_ref), (
                f"{path.relative_to(REPO_ROOT)} has an unpinned `image:` reference "
                f"({image_ref!r}) — container images (job containers, service containers) "
                f"must be pinned by digest or a specific version tag, never a bare "
                f"repository name (which resolves to `:latest`)"
            )
