import { defineConfig, devices } from "@playwright/test";

// Runs against the real Docker stack (db/redis/aether-vault-server) and a real webui dev
// server pointed at it — neither is started by this config. Run
// `docker compose up -d db redis aether-vault-server`, seed data with
// `python webui/e2e/seed_data.py`, then `npm run dev` (or `npm run start` against a build)
// before `npx playwright test`.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false, // both specs read the same seeded server-side data; keep ordering simple
  // The Weight Diff tab resolves up to 30 commits' full Merkle trees via individual sequential
  // requests (no batched server endpoint exists for this yet — see development/Probleme.md) —
  // against a shared dev registry with real history, that alone can take ~15-20s. Running both
  // spec files in parallel workers made this worse via CPU/network contention and caused
  // spurious "element not found" timeouts even though the data was genuinely still loading, not
  // broken. One worker keeps this test run slow-but-deterministic instead of fast-but-flaky.
  workers: 1,
  timeout: 45_000,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
