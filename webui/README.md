# `webui/` — Next.js Dashboard

The browser dashboard launched by `av webui`: commit DAG, branches, ML metrics, storage
stats, the per-layer **Weight Diff** heatmap, and multi-project scoping. Served from the
`aether-vault-webui` container (built by `../Dockerfile.webui`-style compose services) or
plainly via `npm run dev`. See the [main README](../README.md).

## Layout

| Path | Purpose |
|---|---|
| `src/app/` | Next.js App Router entry (`page.tsx`, `layout.tsx`, `globals.css`) |
| `src/components/` | One component per sidebar tab (`CommitsPanel`, `BranchesPanel`, `MetricsPanel`, `StoragePanel`, `WeightDiffPanel`, `ProjectsPanel`, ...) plus shared pieces (`Sidebar`, `TopBar`, `CommitGraph`, `TokenGate`) |
| `src/lib/api.ts` | Typed client for every registry endpoint (incl. `include_layers` aggregate fetch) |
| `src/lib/diffWeights.ts` | Pure per-layer diff logic shared by Weight Diff and tests |
| `src/lib/runDetail.ts` | v1.2.2 pure run-detail helpers: parent-lineage walk, client-side tree-diff summary (chunk reuse + dedup efficiency), metrics-table flattening |
| `src/hooks/useDashboard.ts` | Polling/fetch orchestration with parallel first paint |
| `__tests__/` | Vitest + React Testing Library suites (79 tests, 16 files) |
| `__benchmarks__/speed.bench.ts` | Vitest bench suite exercised by `av test --speed --webui` |
| `e2e/` | Playwright specs + `seed_data.py` (pushes real commits via the real CLI) |

## Commands

```bash
npm test                 # Vitest unit/component suite
npm run dev              # dev server on :3000 (needs a reachable registry)
npx playwright install --with-deps chromium   # once, for E2E
npx playwright test      # E2E against a seeded live stack (docker compose up)
```

## Notes

- Auth: when the registry is in Protected mode, `av webui` hands the token over via a
  one-time `?av_token=` URL; any other entry point shows the `TokenGate` prompt once.
  Per-user tokens work identically — any valid credential issued by `av auth add-user`
  passes the same gate.
- Runs tab (v1.2.0 list; v1.2.2 detail): rows EXPAND into a detail panel — parent-lineage
  chain, linked commits with messages and a metrics table, a client-side semantic summary
  from the last two linked commits' trees, and an env-snapshot pointer. Composed entirely
  from existing endpoints (fetchRun + fetchCommit); live activity badge fed by /api/events.
- Merge visualization: the commit graph draws one edge per parent from the server's
  reconstructed `parents` array (first-parent lane inheritance), so merge commits fork
  on screen; payloads without `parents` fall back to `parent_hash`. Covered by
  `src/components/__tests__/CommitGraph.test.tsx`.
