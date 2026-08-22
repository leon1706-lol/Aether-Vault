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
- Known limitation: the commit graph renders `parent_hash` only — merge commits appear
  linear until the graph learns to draw both parents.
