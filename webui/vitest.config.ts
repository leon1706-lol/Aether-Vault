import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // Pure-logic tests only (lib/diffWeights.ts, lib/api.ts) — no DOM/React needed.
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
