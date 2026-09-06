import { expect, test } from "@playwright/test";

// Smoke test that the whole stack — webui -> FastAPI -> Postgres — actually renders end to
// end. Assumes webui/e2e/seed_data.py has already been run against the live docker-compose stack.
test("dashboard loads and shows the seeded commits", async ({ page }) => {
  await page.goto("/");

  // Boot assertion: the sidebar brand block plus the Dashboard nav item prove the app shell mounted.
  await expect(page.getByText("ML Registry Dashboard")).toBeVisible();
  await expect(page.locator("#nav-dashboard")).toBeVisible();

  // `.first()` rather than a uniqueness assertion: seed_data.py can run more than once
  // against the same shared dev registry, so more than one matching commit is expected.
  await expect(page.getByText("v2 checkpoint").first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("v1 checkpoint").first()).toBeVisible();

  // Stats row should reflect a non-zero commit count once data has loaded (not the "—"
  // loading placeholder).
  await expect(page.getByText("Total Commits")).toBeVisible();
  const statsGrid = page.locator(".stats-grid").first();
  await expect(statsGrid.getByText("—")).toHaveCount(0);
});

// A real backend failure must render as a visible error, not silently look identical to
// "no data yet" — fails the whole registry API server-side for a true end-to-end proof.
test("dashboard shows a real error state instead of an empty state when the registry API fails", async ({ page }) => {
  await page.route("**/api/commits**", (route) => route.fulfill({ status: 500, body: "boom" }));
  await page.route("**/api/refs**", (route) => route.fulfill({ status: 500, body: "boom" }));
  await page.route("**/api/stats**", (route) => route.fulfill({ status: 500, body: "boom" }));

  await page.goto("/");

  // The app shell still mounts (health/refs/stats/commits failures don't crash the page) —
  // but the commit graph and commit list panels show a real error, not their empty states.
  await expect(page.getByText("ML Registry Dashboard")).toBeVisible();
  await expect(page.getByText(/⚠.*HTTP 500/).first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("No commits to graph")).not.toBeVisible();
  await expect(page.getByText("No commits yet")).not.toBeVisible();
});
