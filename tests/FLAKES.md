# Known flakes

v1.3.4 (todo.md item 12): the registry for `@pytest.mark.flaky`. A test may carry that
marker **only** if it has an entry here — `tests/test_flake_registry.py` enforces this
both ways (a marked test with no entry, and an entry with no expiry date, both fail CI).
This is a quarantine with a clock, not a blanket "retry forever and stop looking" escape
hatch: gating CI jobs run `-m "not flaky"`; a separate, non-gating `flaky-quarantine` job
runs `-m flaky` with `continue-on-error: true` so a real regression in one is still
visible in the run summary, never silently swallowed.

**Currently empty, deliberately.** No test in this suite is marked `flaky` as of v1.3.4 —
the two known machine-specific timing flakes documented in
`development/Probleme.md` (#133's `test_cli_commit_pushes_to_a_live_server` /
`test_live_two_repo_clone_pull_flow` opportunistic-live-engine flake, and
`test_perf_gate.py`'s accepted `log()` timing variance) were deliberately left
**unmarked** rather than seeded here on the strength of a single past session's
observation — the "not real code defects" conclusion in that entry only ever came from a
manual repro on ONE machine, not a live-verified reproduction under this registry's own
process. If either recurs in CI (not just locally), add it here with:

| Test id | First seen | Owner | Hypothesis | Expires |
|---|---|---|---|---|
| `tests/test_server.py::TestSomething::test_x` | 2026-09-06 | (who's investigating) | one-line theory, not a shrug | 2026-10-06 |

**Expires** is a hard date, not aspirational — `test_flake_registry.py` fails CI once it
passes, forcing a re-triage rather than letting an entry rot indefinitely. Removing an
entry (because it's fixed, or because it turned out not to be flaky at all) means also
removing its `@pytest.mark.flaky` marker in the same change — the two must never drift
apart.
