import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { TokenGate } from "../TokenGate";

// Regression (webui-e2e token-gate): Next.js patches window.history.replaceState for
// App Router integration, and a strip issued during render (pre-hydration) can be
// OVERRIDDEN when hydration completes — restoring "?av_token=" into the address bar.
// The component must therefore ALSO strip post-mount; this test simulates the override
// by making the FIRST replaceState call restore the original URL (as Next would) and
// asserting the URL is still clean after effects run.
describe("TokenGate av_token stripping", () => {
  const ORIGINAL = "http://localhost:3000/?av_token=owner-browser-secret";
  const CLEAN = "http://localhost:3000/";
  let replaceCalls: string[] = [];

  beforeEach(() => {
    replaceCalls = [];
    window.history.pushState({}, "", ORIGINAL);
    const real = window.history.replaceState.bind(window.history);
    vi.spyOn(window.history, "replaceState").mockImplementation(
      ((state: unknown, unused: string, url?: string | URL | null) => {
        const target = typeof url === "string" ? url : String(url ?? "");
        replaceCalls.push(target);
        if (replaceCalls.length === 1) {
          // Simulate Next restoring the entry URL over the render-phase strip:
          return real(state, unused, ORIGINAL);
        }
        return real(state, unused, url);
      }) as typeof window.history.replaceState,
    );
  });

  afterEach(() => {
    cleanup();
    window.history.replaceState({}, "", CLEAN);
    vi.restoreAllMocks();
  });

  it("strips ?av_token even when the pre-hydration strip is overridden", async () => {
    render(
      <TokenGate>
        <div>dashboard</div>
      </TokenGate>,
    );

    // Render-phase pass ran (overridden in this simulation):
    expect(replaceCalls.length).toBeGreaterThanOrEqual(1);

    // Let the post-mount effect flush:
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));

    expect(window.location.search).not.toContain("av_token");
    // The safety net actually issued its own strip:
    expect(replaceCalls[replaceCalls.length - 1]).toBe(CLEAN);
  });

  it("persists the handoff token to localStorage before children fetch", () => {
    localStorage.removeItem("aether-vault:api-token");
    render(
      <TokenGate>
        <div>dashboard</div>
      </TokenGate>,
    );
    expect(localStorage.getItem("aether-vault:api-token")).toBe(
      "owner-browser-secret",
    );
    localStorage.removeItem("aether-vault:api-token");
  });
});
