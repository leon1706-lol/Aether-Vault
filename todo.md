# To-Do — Objectives Canvas

This is the owner's planning space, not a generated backlog. Whatever is written below is
the current objective(s) and any personal notes/context for it — read it before starting
work in this repo, and treat it as the live brief for what an AI agent should do next.
Expect this file to be rewritten or cleared out entirely as objectives change; it does not
accumulate history (that's what `development/CHANGELOG.md` and `development/Probleme.md`
are for — see `AGENTS.md`).

-----

## Blocked by environment, not by choice

- [ ] **`tests/test_server.py`'s remaining tests, re-attempted 2026-09-13 (2nd session,
      Docker up)**: a one-test-per-subprocess resumable driver got through **79/193** (56
      passed, 23 failed) before the *harness itself* killed the whole background process
      for low system memory — not the driver's own safety check, which had been passing.
      Re-ran one failure alone right after (`test_alembic_brings_schema_to_head`) and it
      **passed clean** — strong evidence the 23 are the same documented class of false
      failure as Probleme.md #167 (a DB fixture's teardown timing out under memory
      pressure, not a real bug), but a retry batch for the other 22 couldn't even start a
      single test before being killed again, so only that one is actually *confirmed* a
      flake. The 22 still-unconfirmed names (all `test_server.py::`, mostly clustered
      around `push_commit`/`list_commits`/`get_ref`/`gc`):
      `test_protected_mode_gates_writes_too_not_just_reads`,
      `test_push_commit_stamps_authenticated_username_as_author`,
      `test_push_commit_respects_explicit_author_from_authenticated_user`,
      `test_owner_shared_secret_stamps_owner_as_author`,
      `test_anonymous_mode_keeps_author_untouched`,
      `test_push_commit_then_get_commit_roundtrip`,
      `test_list_commits_omits_tree_by_default`,
      `test_list_commits_include_layers_matches_get_commit`,
      `test_list_commits_include_layers_handles_a_commit_with_no_tree`,
      `test_list_commits_include_layers_resolves_all_roots_in_one_shared_call`,
      `test_push_commit_duplicate_returns_409`, `test_push_commit_rejects_oversized_tree`,
      `test_push_commit_rejects_too_many_tags`, `test_push_commit_rejects_oversized_tag`,
      `test_push_commit_rejects_too_many_metrics`,
      `test_push_commit_rejects_oversized_message`, `test_update_ref_then_get_ref_roundtrip`,
      `test_list_refs_filters_by_project_id`, `test_dashboard_summary_and_projects_endpoints`,
      `test_gc_respects_grace_period_then_sweeps_when_aged`,
      `test_merge_commit_round_trips_both_parents`,
      `test_single_parent_commit_reports_one_parent`. Re-run these 22 specifically first on
      a future attempt (fastest path to confirming they're all flakes too) before touching
      the other 114 never-yet-reached tests. Root cause unchanged from before: this box's
      free RAM sat at 0.3-0.5GB throughout, mostly consumed by Docker's own `vmmem` WSL VM
      (~490-520MB) plus this session's own `claude` process(es) (up to ~625MB combined) —
      not something a smaller test batch works around, since even a single subprocess
      couldn't get a safe memory floor on the second retry attempt.
- [ ] **The full `development/BENCHMARKS.md` re-capture (still `a73ebde`/v1.4.0.2-era, now
      badly stale)** — attempted same session as above; a memory-checking driver correctly
      refused to even start the *cheapest* of the 9 named benchmarks (`hashing_throughput`,
      no server/subprocess tools needed), measuring 308MB free against its own 400MB safety
      floor. Never actually ran anything, so no data to salvage or resume from — a clean
      re-attempt from scratch once there's real headroom. git-lfs 3.7.1/dvc/mlflow are all
      confirmed present on PATH and the live registry stack is confirmed up+healthy, so
      nothing else blocks this besides free memory.

### Future testing not in scope for current plans

- **A live external IdP run** (Keycloak compose overlay, or a real Okta/Entra tenant) —
  the protocol code (PKCE, JWKS verification, SAML signature/conditions) is implemented
  and tested against this server's own routes, but has not been driven end-to-end
  against a genuinely external IdP in this environment.
- **Real Kubernetes HA drill** — the Helm chart is schema-verified, not cluster-drilled.
- **Reference customers / pilot onboarding kit** — not started (a sales outcome).
- **Third-party security audit / SOC2 / staffed support rotation** — need a firm/hires.
- **Docker image rebuild + post-rebuild verification** — the owner does this manually.
- **Daemon filesystem watcher (fsmonitor-style)** — deliberately deferred past v1.5.0; a
  missed event is a correctness bug, not just a slowness one. If added later it may only
  *shrink* the stat set, never replace it, gated behind a health-flag cookie test.
- **C++ inter-file batch hashing binding** (`hash_files(list[str])`) — the Python-side
  compute/apply pool over GIL-released `hash_file` gets most of this win already; revisit
  only if profiling after v1.5.0 shows it's still worth the added C++ surface.
