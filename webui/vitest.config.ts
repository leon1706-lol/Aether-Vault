import path from "path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    // Mirrors tsconfig.json's "@/*" -> "./src/*" path alias, which components import via
    // (e.g. `import { formatBytes } from "@/lib/api"`) — Vitest doesn't read tsconfig paths
    // on its own.
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  test: {
    // Pure-logic tests (lib/diffWeights.ts, lib/api.ts) need no DOM and run under "node";
    // component tests need a real DOM, so they get "jsdom" instead — set per-glob rather than
    // globally, since jsdom is slower and unnecessary for the pure-logic tests already in place.
    environment: "node",
    environmentMatchGlobs: [["src/components/**", "jsdom"]],
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
