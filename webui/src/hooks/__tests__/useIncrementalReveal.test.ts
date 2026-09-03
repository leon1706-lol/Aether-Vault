// vitest.config.ts's environmentMatchGlobs picks up jsdom for this directory (renderHook
// needs a DOM + requestAnimationFrame).
import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useIncrementalReveal } from "../useIncrementalReveal";

describe("useIncrementalReveal", () => {
  it("reveals everything immediately when total is within the initial batch", () => {
    const { result } = renderHook(() => useIncrementalReveal(3, { initialBatch: 500 }));
    expect(result.current).toBe(3);
  });

  it("caps the first paint at initialBatch, then grows to the full total", async () => {
    const { result } = renderHook(() =>
      useIncrementalReveal(2500, { initialBatch: 500, batchSize: 1000 })
    );
    expect(result.current).toBe(500);

    await waitFor(() => expect(result.current).toBe(2500));
  });

  it("never exceeds total even when batchSize overshoots it", async () => {
    const { result } = renderHook(() =>
      useIncrementalReveal(1200, { initialBatch: 500, batchSize: 1000 })
    );
    await waitFor(() => expect(result.current).toBe(1200));
    expect(result.current).toBeLessThanOrEqual(1200);
  });

  it("resets to the initial batch when total shrinks (e.g. a new file is picked)", async () => {
    const { result, rerender } = renderHook(
      ({ total }: { total: number }) => useIncrementalReveal(total, { initialBatch: 5 }),
      { initialProps: { total: 20 } }
    );
    await waitFor(() => expect(result.current).toBe(20));

    rerender({ total: 3 });
    expect(result.current).toBe(3);
  });

  it("returns 0 for an empty set", () => {
    const { result } = renderHook(() => useIncrementalReveal(0));
    expect(result.current).toBe(0);
  });
});
