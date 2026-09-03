"use client";

import { useEffect, useState } from "react";

/**
 * v1.3.0 (todo.md item 25): progressively reveals `total` items in batches via
 * requestAnimationFrame, replacing the old hard `MAX_RENDERED_LAYERS` cutoff (see
 * lib/diffWeights.ts) that permanently hid the tail of a checkpoint with more tensors
 * than the cap — every layer now eventually renders, it just fills in over a handful of
 * frames instead of blocking the main thread with one giant synchronous paint (the
 * actual reason the cap existed). `initialBatch` renders immediately with no frame delay
 * so small diffs still look instant; `batchSize` controls the growth step thereafter.
 *
 * Deliberately two separate effects rather than one self-rescheduling rAF loop: scheduling
 * the next frame from inside setState's functional updater mixes a side effect into what
 * must stay a pure function, which is unreliable under React's dev-mode double-invocation
 * of updaters. Instead, the reveal effect below re-runs (idiomatically) each time
 * `visibleCount` itself changes, chaining one rAF tick at a time via the dependency array.
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
