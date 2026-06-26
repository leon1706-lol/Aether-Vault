import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

import "@testing-library/jest-dom/vitest";

// Without `test.globals: true` in vitest.config.ts, React Testing Library's automatic
// afterEach-cleanup (which it normally self-registers against a global `afterEach`) never
// gets wired up — each test's rendered DOM nodes were leaking into the next test in the same
// file, causing spurious "found multiple elements" failures. Register cleanup explicitly here
// instead of opting into vitest's globals just for this.
afterEach(() => {
  cleanup();
});

// jsdom doesn't implement ResizeObserver, which recharts' <ResponsiveContainer> needs to
// measure its container — without a stub, mounting any chart component throws
// "ReferenceError: ResizeObserver is not defined" before a test even gets to its assertions.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

if (!("ResizeObserver" in globalThis)) {
  // A plain cast, not `@ts-expect-error` — whether this line is "the DOM lib type already
  // covers it" or not depends on which tsconfig/TS version is doing the checking (this file is
  // only ever run by Vitest, but `next build`'s project-wide type-check still visits it and
  // disagreed once already: an `@ts-expect-error` is itself a type error if TS doesn't think
  // there's anything to suppress, and that broke the production Docker build).
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub;
}
