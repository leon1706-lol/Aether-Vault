import { expect, test } from "@playwright/test";

// Smoke test that the whole stack — webui -> FastAPI -> Postgres — actually renders end to
// end, not just at the unit/component level. Assumes webui/e2e/seed_data.py has already been
// run against the live docker-compose stack (db/redis/aether-vault-server).
test("dashboard loads and shows the seeded commits", async ({ page }) => {
  await page.goto("/");

  // Boot assertion: the sidebar brand block ("ML Registry Dashboard") plus the Dashboard
  // nav item prove the app shell mounted. (An older layout had a "🌌 Aether-Vault" hero
  // heading; the brand is now an image + text in the sidebar, so role=heading never matches.)
  await expect(page.getByText("ML Registry Dashboard")).toBeVisible();
  await expect(page.locator("#nav-dashboard")).toBeVisible();

  // The dashboard polls /api/dashboard data on mount — wait for a seeded commit message rather
  // than asserting immediately, since the initial render is the loading state. `.first()`
  // rather than a uniqueness assertion: seed_data.py can be (and was, while debugging this
  // spec) run more than once against the same shared dev registry, so more than one matching
  // commit existing is expected and not itself a failure.
  await expect(page.getByText("v2 checkpoint").first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("v1 checkpoint").first()).toBeVisible();

  // Stats row should reflect a non-zero commit count once data has loaded (not the "—"
  // loading placeholder).
  await expect(page.getByText("Total Commits")).toBeVisible();
  const statsGrid = page.locator(".stats-grid").first();
  await expect(statsGrid.getByText("—")).toHaveCount(0);
});
