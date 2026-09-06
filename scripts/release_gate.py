"""v1.3.0 (todo.md item 30): the `gate` job's checks, factored out of release.yml so
they're unit-testable (tests/test_release_gate.py) instead of living only as bash in a
workflow file no test suite ever exercises. Every publish job in release.yml (`publish-pypi`,
`github-release`, `build-and-push-docker`) depends on the `gate` job succeeding — this
script IS that job's substance. Read-only toward PRs by construction: every check here
only reads state (git, the filesystem, `gh api`) and exits non-zero to block the release;
none of them ever merge, approve, open, or push anything (see tests/test_ci_policy.py's
standing no-bots/no-auto-merge guard, which this script must never violate).

Usage: python scripts/release_gate.py --tag vX.Y.Z [--repo-root PATH] [--skip-gh-check]
                                        [--skip-tests] [--report PATH]
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
    itself (a very old but still-ancestor sha would still pass on its own — see the
    stricter MINOR-release check below, which this one feeds into) — combined with the
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
    if result.returncode != 0:
        return False, (
            f"BENCHMARKS.md's captured commit {sha} is not an ancestor of {tag} ({result.stderr.strip()}) "
            "— re-run `av benchmark --markdown development/BENCHMARKS.md` against this release"
        )
    return True, f"BENCHMARKS.md's captured commit {sha} is an ancestor of {tag}"


def _parse_semver(tag: str) -> tuple[int, ...]:
    return tuple(int(p) for p in tag.lstrip("v").split(".") if p.isdigit())


def _previous_tag(repo_root: Path, tag: str) -> str | None:
    """The most recent tag reachable from `tag`'s own parent — i.e. "whatever was tagged
    right before this one". None when `tag` is the very first tag in the repo (nothing
    to compare against, so every freshness/sync check below treats that as vacuously ok
    rather than failing a release that has no predecessor to be out of sync with)."""
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", f"{tag}^"],
        cwd=repo_root, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _is_minor_or_above(repo_root: Path, tag: str) -> bool:
    prev = _previous_tag(repo_root, tag)
    if prev is None:
        return True
    return _parse_semver(tag)[:2] != _parse_semver(prev)[:2]


def check_benchmarks_fresh_on_minor(repo_root: Path, tag: str) -> tuple[bool, str]:
    """todo.md item 22: on a MINOR-or-above release, the ancestor check above ISN'T
    enough on its own — an ancient-but-still-ancestor captured sha (say, from three
    releases ago) passes it trivially. This additionally requires the captured commit to
    be STRICTLY NEWER than whatever the PREVIOUS tag's own benchmarks were captured
    against (i.e. re-measured at least once since then), OR an explicit
    `Benchmarks: unchanged` attestation line in this release's own CHANGELOG entry for
    the cases where nothing perf-relevant actually changed. A PATCH-only release is
    exempt entirely — VERSIONING.md's own bump table already scopes PATCH to
    "bug fixes, perf, docs... — safe", not something this gate should force a
    re-benchmark for."""
    if not _is_minor_or_above(repo_root, tag):
        return True, f"{tag} is PATCH-only (no MINOR/MAJOR change) — benchmark freshness not required"

    changelog_path = repo_root / "development" / "CHANGELOG.md"
    if changelog_path.exists() and "Benchmarks: unchanged" in changelog_path.read_text(encoding="utf-8").split("## ")[-1]:
        return True, "latest CHANGELOG entry explicitly attests 'Benchmarks: unchanged'"

    prev = _previous_tag(repo_root, tag)
    if prev is None:
        return True, f"{tag} has no previous tag to compare benchmark freshness against"

    bench_path = repo_root / "development" / "BENCHMARKS.md"
    if not bench_path.exists():
        return False, f"{bench_path} does not exist"
    match = re.search(r"Aether-Vault @ `([0-9a-f]{7,40})`", bench_path.read_text(encoding="utf-8"))
    if not match:
        return False, f"{bench_path} has no '**Captured:** ..., Aether-Vault @ `<sha>`, ...' line"
    sha = match.group(1)

    # Strictly newer than the PREVIOUS tag means: an ancestor of THIS tag (already
    # checked above), but NOT an ancestor of the previous one.
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, prev],
        cwd=repo_root, capture_output=True, text=True,
    )
    if result.returncode == 0:
        return False, (
            f"{tag} is a MINOR-or-above release but BENCHMARKS.md's captured commit {sha} "
            f"already predates the PREVIOUS tag ({prev}) — re-run `av benchmark --markdown "
            f"development/BENCHMARKS.md` for this release, or add an explicit "
            f"'Benchmarks: unchanged' line to this release's CHANGELOG entry if nothing "
            f"perf-relevant actually changed"
        )
    return True, f"BENCHMARKS.md's captured commit {sha} postdates the previous tag ({prev})"


def check_changelog_versioning_sync(repo_root: Path, tag: str) -> tuple[bool, str]:
    """todo.md item 21: refuse a tag if CHANGELOG.md/VERSIONING.md are out of sync with
    it. (1) the newest CHANGELOG phase entry's own header must mention this release's
    version. (2) on a MINOR-or-above release, VERSIONING.md must have a matching
    '## v<version> additive surfaces' section — this repo's own established convention
    (see the real v1.3.1/v1.3.2/v1.3.3 sections), keyed to the FULL tag version, not just
    major.minor: this project has repeatedly shipped genuinely additive surfaces under a
    patch-position version number (VERSIONING.md's own documented v1.3.1 policy
    exception), so every MINOR-or-above tag gets its own section by established practice,
    not one shared per major.minor."""
    changelog_path = repo_root / "development" / "CHANGELOG.md"
    if not changelog_path.exists():
        return False, f"{changelog_path} does not exist"
    text = changelog_path.read_text(encoding="utf-8")
    headers = list(re.finditer(r"^## .+$", text, re.MULTILINE))
    if not headers:
        return False, f"{changelog_path} has no '## Phase N' entries at all"
    latest_header = headers[-1].group().strip()
    bare = tag.lstrip("v")
    if bare not in latest_header and tag not in latest_header:
        return False, (
            f"the latest CHANGELOG entry ({latest_header!r}) does not mention this "
            f"release's version ({bare!r}) — update its Phase header before tagging"
        )

    if _is_minor_or_above(repo_root, tag):
        versioning_path = repo_root / "VERSIONING.md"
        if not versioning_path.exists():
            return False, f"{versioning_path} does not exist"
        v_text = versioning_path.read_text(encoding="utf-8")
        if not re.search(rf"^## v{re.escape(bare)}\b", v_text, re.MULTILINE):
            return False, (
                f"{tag} is a MINOR-or-above release but VERSIONING.md has no "
                f"'## v{bare} additive surfaces' section — document the new surfaces "
                f"before tagging (see the existing v1.3.1/v1.3.2/v1.3.3 sections)"
            )
    return True, f"CHANGELOG mentions {tag}; VERSIONING.md documents it (if MINOR-or-above)"


def _fetch_required_contexts_live(repo: str, gh_token: str | None) -> list[str] | None:
    """The live `required_status_checks.contexts` list on `master` — the actual set
    branch protection enforces right now. Returns None (not an empty list — a real
    "master requires zero checks" is implausible and would otherwise silently make
    `check_required_checks_green` vacuously pass) on any failure, so the caller falls
    back to `.github/required-checks.txt`."""
    import urllib.error
    import urllib.request

    url = f"https://api.github.com/repos/{repo}/branches/master/protection/required_status_checks"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if gh_token:
        req.add_header("Authorization", f"Bearer {gh_token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None
    contexts = data.get("contexts")
    return contexts or None


def _required_contexts(repo_root: Path, repo: str, gh_token: str | None) -> tuple[list[str], str]:
    """Resolves the list of check-run names a release must see green for, live branch
    protection preferred, `.github/required-checks.txt` as the read-only fallback.
    Returns (contexts, source_description)."""
    live = _fetch_required_contexts_live(repo, gh_token)
    if live:
        return live, "live branch-protection required_status_checks"

    fallback_path = repo_root / ".github" / "required-checks.txt"
    if not fallback_path.exists():
        return [], f"neither the live branch-protection API nor {fallback_path} was available"
    lines = [
        line.strip() for line in fallback_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return lines, f"fallback file {fallback_path.relative_to(repo_root)} (live API unavailable)"


def check_required_checks_green(repo: str, tag: str, gh_token: str | None,
                                 repo_root: Path) -> tuple[bool, str]:
    """v1.3.4 (todo.md item 20, W4a): the ORIGINAL version of this check
    (`check_tagged_commit_tests_green`) filtered check-runs by `"test" in name.lower()` —
    which silently ignored `ha-drill`, `e2e-suite`, `chaos-drills`, `helm-lint`,
    `e2e-engine-smoke`, `package-build`, every `security.yml` job, and everything CodeQL
    adds. A release could ship with any of those genuinely red and this check would still
    pass. Now requires EVERY context the live (or fallback) required-checks list names,
    not a name-substring guess. Read-only (GET only) — never touches a PR."""
    import urllib.error
    import urllib.request

    required, source = _required_contexts(repo_root, repo, gh_token)
    if not required:
        return False, f"could not resolve a required-checks list at all ({source})"

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
    by_name: dict[str, list[dict]] = {}
    for r in runs:
        by_name.setdefault(r.get("name", ""), []).append(r)

    problems = []
    for name in required:
        matches = by_name.get(name, [])
        if not matches:
            problems.append(f"{name}: no check-run found")
            continue
        # The MOST RECENT run of that name (a re-run replaces, not appends).
        latest = max(matches, key=lambda r: r.get("started_at") or "")
        if not (latest.get("status") == "completed" and latest.get("conclusion") == "success"):
            problems.append(f"{name}: {latest.get('conclusion') or latest.get('status')}")

    if problems:
        return False, (
            f"{len(problems)}/{len(required)} required check(s) not green for {tag} "
            f"(required-checks source: {source}): {'; '.join(problems)}"
        )
    return True, f"all {len(required)} required check-run(s) green for {tag} (required-checks source: {source})"


def run_stack_free_suite(repo_root: Path) -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        cwd=repo_root, capture_output=True, text=True,
    )
    tail = "\n".join(result.stdout.strip().splitlines()[-15:])
    if result.returncode == 0:
        return True, f"stack-free suite passed\n{tail}"
    return False, f"stack-free suite failed (exit {result.returncode})\n{tail}"


def write_report(report_path: Path, tag: str, checks: list[tuple[str, tuple[bool, str]]]) -> None:
    """todo.md item 23: the Essential-Tasks/gate outcome as an ARTIFACT, not just a claim
    in a workflow log — attached to the GitHub Release itself (github-release job) so the
    sign-off is evidence a later reader can actually open, not just trust."""
    import datetime

    lines = [
        f"# Release gate report — {tag}",
        "",
        f"Generated: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    overall_ok = True
    for name, (ok, detail) in checks:
        overall_ok = overall_ok and ok
        status = "✅ PASS" if ok else "❌ FAIL"
        # Table cells can't contain raw newlines or pipes.
        safe_detail = detail.replace("\n", "<br>").replace("|", "\\|")
        lines.append(f"| {name} | {status} | {safe_detail} |")
    lines.append("")
    lines.append(f"**Overall: {'PASSED' if overall_ok else 'FAILED'}**")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    parser.add_argument("--report", type=Path, default=None,
                        help="Write a Markdown report of every check's outcome to this path (todo.md item 23)")
    args = parser.parse_args()

    import os
    gh_token = args.gh_token or os.environ.get("GITHUB_TOKEN")

    checks: list[tuple[str, tuple[bool, str]]] = []
    if not args.skip_tests:
        checks.append(("stack-free suite", run_stack_free_suite(args.repo_root)))
    if not args.skip_gh_check:
        checks.append(("required checks green", check_required_checks_green(args.repo, args.tag, gh_token, args.repo_root)))
    checks.append(("perf-history.json has this release", check_perf_history_has_tag(args.repo_root, args.tag)))
    checks.append(("BENCHMARKS.md captured sha is current", check_benchmarks_captured_sha_is_an_ancestor(args.repo_root, args.tag)))
    checks.append(("BENCHMARKS.md is fresh (MINOR-or-above releases)", check_benchmarks_fresh_on_minor(args.repo_root, args.tag)))
    checks.append(("CHANGELOG.md is signed off", check_changelog_has_signed_off_entry(args.repo_root)))
    checks.append(("CHANGELOG.md / VERSIONING.md are in sync with this tag", check_changelog_versioning_sync(args.repo_root, args.tag)))

    failed = False
    for name, (ok, detail) in checks:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}: {detail}")
        if not ok:
            failed = True

    if args.report:
        write_report(args.report, args.tag, checks)
        print(f"\nReport written to {args.report}")

    if failed:
        print("\nRelease gate FAILED — publish jobs will not run. Fix the above and re-tag.")
        return 1
    print("\nRelease gate PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
