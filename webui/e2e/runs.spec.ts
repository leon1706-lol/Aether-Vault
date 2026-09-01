import { expect, test } from "@playwright/test";

// v1.2.5: Runs tab detail view + deep linking, against the run seeded by
// webui/e2e/seed_data.py's seed_run() (3 commits with a loss metric, tagged with a
// distinctive name so this test isn't thrown off by unrelated runs already in the
// shared dev registry from past manual-testing sessions).
const RUN_NAME = "e2e-runs-spec-run";

test("Runs: opening a row shows the dedicated detail panel", async ({ page }) => {
  await page.goto("/?tab=runs");

  const row = page.locator("tr", { hasText: RUN_NAME });
  await expect(row.first()).toBeVisible({ timeout: 15_000 });
  await row.first().click();

  await expect(page.getByText(/Linked commits/)).toBeVisible();
  await expect(page.getByText(/Latest change \(semantic\)/)).toBeVisible();
  // 3 commits with a numeric metric each meets the chart threshold (>=3) — the run
  // detail panel should render the metrics history chart, not the plain table.
  await expect(page.getByText("ML Metrics Over Time")).toBeVisible();

  // Opening the row put the run id in the URL (deep-linkable, shareable).
  await expect(page).toHaveURL(/[?&]run=/);

  // Closing clears both the panel and the URL param.
  await page.getByRole("button", { name: "Close" }).click();
  await expect(page.getByText(/Linked commits/)).not.toBeVisible();
  await expect(page).not.toHaveURL(/[?&]run=/);
});

test("Runs: a deep-linked URL opens the run detail directly on load", async ({ page, request }) => {
  // Resolve the seeded run's real (server-generated) id via the API — the deep-link
  // contract is "reload this exact URL and land on the same run", so this test needs a
  // real id, not the display name.
  const runsResp = await request.get("http://localhost:8000/api/runs?limit=200");
  expect(runsResp.ok()).toBeTruthy();
  const { runs } = await runsResp.json();
  const seeded = (runs as Array<{ id: string; name: string | null }>).find(
    (r) => r.name === RUN_NAME
  );
  expect(seeded, `expected a seeded run named ${RUN_NAME}`).toBeTruthy();

  await page.goto(`/?tab=runs&run=${seeded!.id}`);

  // No click needed — the panel opens straight from the URL on first paint.
  await expect(page.getByText(/Linked commits/)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(new RegExp(seeded!.id.slice(0, 8)))).toBeVisible();
});
