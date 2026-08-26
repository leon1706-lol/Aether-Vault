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
  `include_layers` aggregate fetch); token lives in localStorage, not build time.
- `src/lib/diffWeights.ts` - pure per-layer diff logic shared by Weight Diff + tests.
- `src/lib/runDetail.ts` - pure run-detail helpers: parent-lineage walk, client-side
  tree-diff summary (chunk reuse + dedup efficiency), metrics-table flattening.
- `src/hooks/useDashboard.ts` - polling orchestration with parallel first paint.
- `__tests__/` - Vitest + React Testing Library suites (101 tests, 20 files).
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
- Runs detail composes lineage/metrics/semantic summary entirely client-side from
  existing endpoints - deliberately no dedicated server endpoint.
- Commit graph draws one edge per parent from the reconstructed `parents` array
  (first-parent lane inheritance); payloads without `parents` fall back.
