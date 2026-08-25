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
  Per-user tokens work identically — any valid credential issued by `av auth add-user`
  passes the same gate.
- Runs tab (v1.2.0): lists registry runs with status colors, metric summaries, and a
  live activity badge fed by /api/events polling; see RunsPanel.tsx + api.ts fetchRuns.
- Merge visualization: the commit graph draws one edge per parent from the server's
  reconstructed `parents` array (first-parent lane inheritance), so merge commits fork
  on screen; payloads without `parents` fall back to `parent_hash`. Covered by
  `src/components/__tests__/CommitGraph.test.tsx`.
