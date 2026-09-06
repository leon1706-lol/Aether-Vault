"use client";

import { useEffect, useState } from "react";

/**
 * Progressively reveals `total` items in batches via requestAnimationFrame instead of
 * blocking the main thread with one giant synchronous paint. `initialBatch` renders
 * immediately so small diffs still look instant; `batchSize` controls the growth step.
 *
 * Deliberately two separate effects rather than one self-rescheduling rAF loop:
 * scheduling the next frame from inside setState's functional updater would mix a side
 * effect into what must stay a pure function. Instead the reveal effect re-runs each
 * time `visibleCount` itself changes, chaining one rAF tick at a time.
 */
export function useIncrementalReveal(
  total: number,
  { initialBatch = 500, batchSize = 1000 }: { initialBatch?: number; batchSize?: number } = {}
): number {
  const [visibleCount, setVisibleCount] = useState(() => Math.min(total, initialBatch));

  // Resets whenever the underlying item count changes (a new file/diff was picked).
  useEffect(() => {
    setVisibleCount(Math.min(total, initialBatch));
  }, [total, initialBatch]);

  // Advances one batch per animation frame until visibleCount catches up to total.
  useEffect(() => {
    if (visibleCount >= total) return;
    const frame = requestAnimationFrame(() => {
      setVisibleCount((prev) => Math.min(total, prev + batchSize));
    });
    return () => cancelAnimationFrame(frame);
  }, [visibleCount, total, batchSize]);

  return visibleCount;
}
