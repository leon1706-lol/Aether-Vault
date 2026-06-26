import { expect, test } from "@playwright/test";

// Exercises the Weight Diff tab against the two commits seeded by webui/e2e/seed_data.py
// (layer1 unchanged, layer2 changed between the seeded v1 -> v2). Selects checkpoints by click
// (not drag-and-drop, for CI reliability) using a distinctive filename
// ("e2e-weight-diff.safetensors") so this test isn't thrown off by unrelated checkpoints that
// may already exist in the shared dev registry from past manual-testing sessions.
test("Weight Diff: comparing the seeded checkpoints renders a real layer diff", async ({ page }) => {
  await page.goto("/");
  await page.click("#nav-weight-diff");

  // Generous timeout: the checkpoint list resolves every recent commit's full tree via
  // individual sequential requests (no batched endpoint exists for this yet), which against a
  // shared dev registry with real history can genuinely take ~15-20s — not a sign of failure.
  const seededRows = page.locator(".checkpoint-row", { hasText: "e2e-weight-diff.safetensors" });
  await expect(seededRows.first()).toBeVisible({ timeout: 30_000 });
  await expect(seededRows).toHaveCount(2); // exactly the v1 + v2 seeded rows

  // Rows render oldest -> newest, so the first of these two is v1 and the second is v2.
  await seededRows.nth(0).click(); // fills Slot A
  await seededRows.nth(1).click(); // fills Slot B

  // "Comprehensive Comparison" card should now show a real diff (not the "Drop two
  // checkpoints to compare." placeholder).
  await expect(page.getByText("Drop two checkpoints to compare.")).not.toBeVisible();
  await expect(page.getByText("Total Layers")).toBeVisible();

  // 2 real tensor layers (layer1, layer2) — the synthetic "__header__" pseudo-layer is
  // filtered out of the diff entirely (see lib/diffWeights.ts).
  const statsGrid = page.locator(".stats-grid").first();
  await expect(statsGrid.getByText("2", { exact: true })).toBeVisible(); // Total Layers
  await expect(statsGrid.getByText("1", { exact: true })).toBeVisible(); // Changed (layer2 only)

  // The heatmap should render exactly 2 layer cells: one unchanged, one changed.
  const cells = page.locator(".layer-cell");
  await expect(cells).toHaveCount(2);
  await expect(page.locator(".layer-cell--unchanged")).toHaveCount(1);
  await expect(page.locator(".layer-cell--changed")).toHaveCount(1);
});
