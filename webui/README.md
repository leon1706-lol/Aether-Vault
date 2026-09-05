# webui

Owns the Next.js App Router dashboard launched by `av webui`: commit DAG, branches,
ML metrics, storage stats, the per-layer Weight Diff heatmap, experiment Runs with an
expandable detail view, multi-project scoping, and the Protected-mode TokenGate.
Served from the engine container's standalone bundle or plainly via `npm run dev`.

- `src/app/` - router entry (`page.tsx`, `layout.tsx`, `globals.css`).
- `src/components/` - one component per sidebar tab (`CommitsPanel`, `BranchesPanel`,
  `MetricsPanel`, `StoragePanel`, `WeightDiffPanel`, `RunsPanel`, `ProjectsPanel`)
  plus shared pieces (`Sidebar`, `TopBar`, `CommitGraph`, `TokenGate`).
- `src/lib/api.ts` - typed client for every registry endpoint (incl. the
  `include_layers` aggregate fetch, `fetchRunMetrics()`'s cursor-paginated full history
  from `GET /api/runs/{id}/metrics`, and the `policy_outcome` field on `Run`); token
  lives in localStorage, not build time.
- `src/lib/diffWeights.ts` - pure per-layer diff logic shared by Weight Diff + tests.
- `src/lib/runDetail.ts` - pure run-detail helpers: parent-lineage walk, client-side
  tree-diff summary (chunk reuse + dedup efficiency), metrics-table flattening.
- `src/hooks/useDashboard.ts` - polling orchestration with parallel first paint;
  surfaces a real `error` state (401/500/offline) distinct from "genuinely empty" -
  every panel reads it and renders a distinct error state with retry (v1.3.0).
- `src/hooks/useIncrementalReveal.ts` - progressive layer reveal for WeightHeatmap /
  LayerDriftChart (v1.3.0), replacing the old hard `MAX_RENDERED_LAYERS` truncation.
- `__tests__/` - Vitest + React Testing Library suites (180 tests, 27 files). Run `npm test` for the current count — not auto-synced here the way `av test`'s own pytest count is (see `python/av_cli/cmd_devtools.py::_update_readme_test_badge`), since `av test --webui` streams Vitest's output live via `subprocess.run` rather than capturing it for parsing; a deliberate, stated scope limit, not an oversight.
- `__benchmarks__/speed.bench.ts` - bench suite exercised by `av test --speed --webui`.
- `e2e/` - Playwright specs + `seed_data.py` (pushes real commits via the real CLI).

## Commands

```bash
npm test                 # Vitest unit/component suite
npm run lint             # next lint --max-warnings 0
npm run typecheck        # tsc --noEmit
npx playwright install --with-deps chromium   # once, for E2E
npx playwright test      # E2E against a seeded live stack (docker compose up)
```

## Notes

- Protected mode: `av webui` hands the token over via a one-time `?av_token=` URL;
  TokenGate consumes it render-phase, strips it (render-phase + post-hydration safety
  net - Next patches history.replaceState and can undo a pre-hydration strip), and
  persists it to localStorage. Any other entry shows the manual prompt once.
- Runs detail (v1.3.0: a full view swap when `?run=` is set, not just an expand-row)
  fetches its full metrics history from the dedicated `GET /api/runs/{id}/metrics`
  endpoint (only when `summary.total_commits > summary.commits.length` - the inline
  `/summary` copy stays capped) and shows a policy-outcome badge; lineage/semantic-diff
  summary composition is still client-side from existing endpoints.
- Weight diff has shareable link state (`?tab=weight-diff&a=&b=&path=`) and an
  arbitrary two-commit compare via a hash input in `CheckpointPicker` (v1.3.0) -
  not just the 100 most recent commits. Cross-linked from `RunsPanel`'s
  "Compare weights" button.
- Commit graph draws one edge per parent from the reconstructed `parents` array
  (first-parent lane inheritance); payloads without `parents` fall back.
