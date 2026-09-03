# To-Do — Objectives Canvas

This is the owner's planning space, not a generated backlog. Whatever is written below is
the current objective(s) and any personal notes/context for it — read it before starting
work in this repo, and treat it as the live brief for what an AI agent should do next.
Expect this file to be rewritten or cleared out entirely as objectives change; it does not
accumulate history (that's what `development/CHANGELOG.md` and `development/Probleme.md`
are for — see `AGENTS.md`).

---

## v1.3.0 wrap-up: everything is done except the actual commit

The full "Depth to 10/10" plan (WP-0 through WP-28, all 28 work packages) is implemented,
tested, and manually verified end to end — nothing deferred. The mechanical wrap-up
checklist is also complete: full `pytest tests/ -v` (Docker stopped per your instruction —
815 passed, 139 skipped as expected, 1 known non-regression noted below), webui lint +
typecheck both green, `development/CHANGELOG.md` Phase 58 entry written (ends with
`Essential-Tasks: signed off`), Obsidian vault regenerated (`generate_code_graph.py` then
`regenerate_vault.py`, both `--append-handoff`), `git status --short` reviewed — 146 files,
all accounted for, nothing stray, no `.env` drift.

**What's left is only step 7 of the checklist: ask before committing (AGENTS.md
non-negotiable).** Nothing has been committed or pushed. When you're ready:
- One commit for the whole v1.3.0 release, Sonnet 5 attribution.
- Push, watch `tests.yml` + `nightly.yml` + the new `chaos-drills` job.
- Tag `v1.3.0` only after the `gate` job passes (it will also need a *fresh*
  `av benchmark` + `scripts/append_perf_history.py` run against the actual tagged commit
  first — the perf-history entries captured so far predate the tag and won't satisfy the
  gate's version check on their own; this is expected, not a bug).

### Two things worth deciding, not urgent

- **Live registry's resting auth mode is genuinely unknown.** This cycle's live
  verification stopped/restarted the engine container repeatedly; whether Anonymous mode
  (its state when Docker was last up) matches what it should be "at rest" was never
  independently confirmed. Not blocking the commit — just don't assume it's settled.
- One pre-existing, non-regression test result: `tests/test_perf_gate.py`'s `log()` probe
  fails locally on this machine (disk I/O characteristic — reproduced in isolation,
  confirmed unrelated to any code touched this cycle, predicted by the plan itself).
  Expected to pass on CI's Linux runners; left as-is deliberately rather than loosening
  the gate to chase local noise. Full reasoning in Probleme.md / the Phase 58 entry.

Every bug found and fixed this cycle (#114–124, including tonight's `av webhooks
deliveries` crash caught specifically because Docker was intentionally off) is logged in
`development/Probleme.md` with severity, fix, and verification.
