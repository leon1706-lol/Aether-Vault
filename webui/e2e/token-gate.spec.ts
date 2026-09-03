import { expect, test } from "@playwright/test";

// Browser-level Protected-mode coverage. Runs AFTER dashboard/weight-diff specs against
// the SAME webui build and the SAME seeded database — the workflow restarts the server in
// Protected mode (AV_API_TOKEN + AV_AUTH_USERS) before this file executes.
//
// Covers the wiring no component test can: ?av_token= consumption → localStorage
// persistence → URL stripped → authenticated data renders; and a cleared browser showing
// the manual entry prompt instead of registry data.

const TOKEN_KEY = "aether-vault:api-token";
const OWNER_TOKEN = "owner-browser-secret";

// Every API response the page produces, echoed into the Playwright log — turns any
// failure into a self-diagnosing one (status codes are what the CORS/#75 bug class hid).
test.beforeEach(async ({ page }, testInfo) => {
  page.on("response", (r) => {
    if (r.url().includes(":8000")) {
      console.log(`[net] ${testInfo.title} :: ${r.status()} ${r.request().method()} ${r.url()}`);
    }
  });
});

test("av_token= handoff: consumed, stripped from URL, persisted, dashboard renders", async ({
  page,
}) => {
  await page.goto(`/?av_token=${OWNER_TOKEN}`);

  // TokenGate strips the query param on mount — it must not linger in history/address bar.
  await expect(page).toHaveURL((u) => !u.search.includes("av_token"));

  // Persisted for every later fetch/navigation in this browser profile.
  const stored = await page.evaluate((k) => window.localStorage.getItem(k), TOKEN_KEY);
  expect(stored).toBe(OWNER_TOKEN);

  // Authenticated data actually renders through the gate (seeded content from earlier specs).
  await expect(page.getByText("ML Registry Dashboard")).toBeVisible();
  await expect(page.getByText("v2 checkpoint").first()).toBeVisible({ timeout: 15_000 });
});

test("second visit without the param stays unlocked via localStorage", async ({ page }) => {
  await page.goto(`/?av_token=${OWNER_TOKEN}`);
  await expect(page.getByText("v2 checkpoint").first()).toBeVisible({ timeout: 15_000 });

  await page.goto("/");
  await expect(page.getByText("v2 checkpoint").first()).toBeVisible({ timeout: 15_000 });
  // No entry prompt may appear while a valid stored token exists.
  await expect(page.getByText("This registry is protected")).toHaveCount(0);
});

test("unknown browser shows the entry prompt instead of registry data", async ({ page }) => {
  await page.goto("/");
  // First fetches fire without a token → 401 → setUnauthorizedHandler shows the prompt.
  // { exact: true } (v1.3.0, Probleme #127): WP-18's per-panel error states now render the
  // SAME UnauthorizedError message verbatim ("This registry is protected — a valid access
  // token is required.") in every panel that 401s, alongside TokenGate's own shorter title
  // ("This registry is protected", no trailing text) — a real, live dashboard genuinely
  // shows both simultaneously (the panels underneath the gate keep fetching and 401ing).
  // A substring locator now matches all of them; only an exact match on TokenGate's own
  // title text is still unambiguous.
  await expect(
    page.getByText("This registry is protected", { exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Manual path: enter the token → reload → same unlocked state as the handoff flow.
  await page.locator('input[type="password"]').fill(OWNER_TOKEN);
  await page.getByRole("button", { name: "Continue" }).click();

  await expect(page.getByText("v2 checkpoint").first()).toBeVisible({ timeout: 15_000 });
});
