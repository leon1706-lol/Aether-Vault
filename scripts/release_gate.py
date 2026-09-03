"""v1.3.0 (todo.md item 30): the `gate` job's checks, factored out of release.yml so
they're unit-testable (tests/test_release_gate.py) instead of living only as bash in a
workflow file no test suite ever exercises. Every publish job in release.yml (`publish-pypi`,
`github-release`, `build-and-push-docker`) depends on the `gate` job succeeding — this
script IS that job's substance. Read-only toward PRs by construction: every check here
only reads state (git, the filesystem, `gh api`) and exits non-zero to block the release;
none of them ever merge, approve, open, or push anything (see tests/test_ci_policy.py's
standing no-bots/no-auto-merge guard, which this script must never violate).

Usage: python scripts/release_gate.py --tag vX.Y.Z [--repo-root PATH] [--skip-gh-check]
Exits 0 if every check passes, 1 (with a clear message naming which check failed) otherwise.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def check_perf_history_has_tag(repo_root: Path, version: str) -> tuple[bool, str]:
    """`development/perf-history.json` must have an entry whose `version` matches the
    release. Tolerant of the leading 'v' a git tag carries but a project version string
    never does (v1.3.0 tag -> "1.3.0" version)."""
    path = repo_root / "development" / "perf-history.json"
    if not path.exists():
        return False, f"{path} does not exist"
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, f"{path} is not valid JSON: {exc}"
    bare = version.lstrip("v")
    versions = [e.get("version", "") for e in history.get("entries", [])]
    if any(v == bare or v == version or v.startswith(bare) for v in versions):
        return True, f"found a perf-history.json entry for version {bare}"
    return False, (
        f"no perf-history.json entry has version {bare!r} (found: {versions}) — run "
        "`python scripts/append_perf_history.py` before tagging (see VERSIONING.md's "
        "release checklist)"
    )


def check_changelog_has_signed_off_entry(repo_root: Path) -> tuple[bool, str]:
    """The MOST RECENT `## Phase N` entry in `development/CHANGELOG.md` must end with the
    literal `Essential-Tasks: signed off` marker. This project APPENDS new entries at the
    BOTTOM of the file (Phase 1 is the first `## ` header, the newest phase is the LAST) —
    so "most recent" means `headers[-1]`, not `headers[0]` (verified against the real file
    before writing this, not assumed from a generic "changelog" convention)."""
    path = repo_root / "development" / "CHANGELOG.md"
    if not path.exists():
        return False, f"{path} does not exist"
    text = path.read_text(encoding="utf-8")
    headers = list(re.finditer(r"^## .+$", text, re.MULTILINE))
    if not headers:
        return False, f"{path} has no '## Phase N' entries at all"
    latest = headers[-1]
    latest_entry = text[latest.end():]
    if "Essential-Tasks: signed off" in latest_entry:
        return True, f"latest CHANGELOG entry ({latest.group().strip()}) is signed off"
    return False, (
        f"the latest CHANGELOG entry ({latest.group().strip()}) has no "
        "'Essential-Tasks: signed off' marker — run the Essential-Tasks checklist end to "
        "end and add the marker before tagging"
    )


def check_benchmarks_captured_sha_is_an_ancestor(repo_root: Path, tag: str) -> tuple[bool, str]:
    """`development/BENCHMARKS.md`'s `**Captured:** ..., Aether-Vault @ \\`<sha>\\`, ...`
    line must name a real commit that's an ancestor of (or equal to) the tag being
    released — proving the captured numbers genuinely predate this release rather than
    being stale from an unrelated branch or, worse, a commit that doesn't even exist in
    this release's history. This is a proxy for "current", not a freshness guarantee by
    itself (a very old but still-ancestor sha would still pass) — combined with the
    perf-history.json check above, which DOES pin down the actual release version."""
    path = repo_root / "development" / "BENCHMARKS.md"
    if not path.exists():
        return False, f"{path} does not exist"
    text = path.read_text(encoding="utf-8")
    match = re.search(r"Aether-Vault @ `([0-9a-f]{7,40})`", text)
    if not match:
        return False, f"{path} has no '**Captured:** ..., Aether-Vault @ `<sha>`, ...' line"
    sha = match.group(1)
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, tag],
        cwd=repo_root, capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True, f"BENCHMARKS.md's captured commit {sha} is an ancestor of {tag}"
    return False, (
        f"BENCHMARKS.md's captured commit {sha} is not an ancestor of {tag} ({result.stderr.strip()}) "
        "— re-run `av benchmark --markdown development/BENCHMARKS.md` against this release"
    )


def check_tagged_commit_tests_green(repo: str, tag: str, gh_token: str | None) -> tuple[bool, str]:
    """Queries GitHub's check-runs API for the tagged commit and requires the `tests.yml`
    workflow's run to have concluded 'success'. Read-only (GET only) — never touches a PR."""
    import urllib.error
    import urllib.request

    url = f"https://api.github.com/repos/{repo}/commits/{tag}/check-runs"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if gh_token:
        req.add_header("Authorization", f"Bearer {gh_token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return False, f"could not query GitHub check-runs for {tag}: {exc}"

    runs = data.get("check_runs", [])
    tests_runs = [r for r in runs if "test" in r.get("name", "").lower()]
    if not tests_runs:
        return False, f"no check-run matching 'test' found for {tag} (saw: {[r.get('name') for r in runs]})"
    failing = [r for r in tests_runs if not (r.get("status") == "completed" and r.get("conclusion") == "success")]
    if failing:
        names = ", ".join(f"{r.get('name')}={r.get('conclusion')}" for r in failing)
        return False, f"not every tests.yml check-run is green for {tag}: {names}"
    return True, f"all {len(tests_runs)} test check-run(s) green for {tag}"


def run_stack_free_suite(repo_root: Path) -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        cwd=repo_root, capture_output=True, text=True,
    )
    tail = "\n".join(result.stdout.strip().splitlines()[-15:])
    if result.returncode == 0:
        return True, f"stack-free suite passed\n{tail}"
    return False, f"stack-free suite failed (exit {result.returncode})\n{tail}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="The release tag being gated, e.g. v1.3.0")
    parser.add_argument("--repo", default="leon1706-lol/aether-vault", help="owner/repo for the GitHub API check")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--gh-token", default=None, help="Token for the GitHub API check (falls back to $GITHUB_TOKEN)")
    parser.add_argument("--skip-gh-check", action="store_true",
                        help="Skip the tagged-commit-is-green GitHub API check (for local dry runs without network/token)")
    parser.add_argument("--skip-tests", action="store_true",
                        help="Skip re-running the stack-free suite (it's what CI's own `test` job just ran — for a fast local dry run)")
    args = parser.parse_args()

    import os
    gh_token = args.gh_token or os.environ.get("GITHUB_TOKEN")

    checks: list[tuple[str, tuple[bool, str]]] = []
    if not args.skip_tests:
        checks.append(("stack-free suite", run_stack_free_suite(args.repo_root)))
    if not args.skip_gh_check:
        checks.append(("tagged commit tests.yml green", check_tagged_commit_tests_green(args.repo, args.tag, gh_token)))
    checks.append(("perf-history.json has this release", check_perf_history_has_tag(args.repo_root, args.tag)))
    checks.append(("BENCHMARKS.md captured sha is current", check_benchmarks_captured_sha_is_an_ancestor(args.repo_root, args.tag)))
    checks.append(("CHANGELOG.md is signed off", check_changelog_has_signed_off_entry(args.repo_root)))

    failed = False
    for name, (ok, detail) in checks:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}: {detail}")
        if not ok:
            failed = True

    if failed:
        print("\nRelease gate FAILED — publish jobs will not run. Fix the above and re-tag.")
        return 1
    print("\nRelease gate PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
