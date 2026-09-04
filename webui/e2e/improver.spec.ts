import { expect, test } from "@playwright/test";

// v1.3.1 (RSI R6, WP-43): the Improver/Regression tabs' live-data proof — webui -> FastAPI
// -> Postgres for the RSI control plane, not just the substrate. Assumes
// webui/e2e/seed_data.py's seed_rsi() has already run against the live stack.
test("Improver tab shows lineage and a pending self-edit", async ({ page }) => {
  await page.goto("/?tab=improver");

  await expect(page.getByText("Improver Lineage")).toBeVisible();
  await expect(page.getByText("Pending Self-Edits")).toBeVisible({ timeout: 15_000 });

  // The seeded pending change set carries this rationale-adjacent risk marker and is
  // never applied/rejected — it should show up in the Pending Self-Edits list.
  await expect(page.getByText("MEDIUM").first()).toBeVisible({ timeout: 15_000 });
});

test("Regression tab shows canary status and the metric-jump anomaly", async ({ page }) => {
  await page.goto("/?tab=regression");

  await expect(page.getByText("Capability Canaries")).toBeVisible();
  await expect(page.getByText("PASS").first()).toBeVisible({ timeout: 15_000 });

  await expect(page.getByText("Improver Churn")).toBeVisible();
  await expect(page.getByText("Anomaly Feed")).toBeVisible();
  await expect(page.getByText("Metric jump").first()).toBeVisible({ timeout: 15_000 });
});
