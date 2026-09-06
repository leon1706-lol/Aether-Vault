import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LayerDriftChart } from "../LayerDriftChart";
import type { LayerDiff } from "@/lib/diffWeights";

// recharts' <ResponsiveContainer> resolves to a 0x0 box under jsdom (no real layout engine),
// so it renders nothing internally even with the ResizeObserver stub from vitest.setup.ts.
// These tests stick to what's verifiable without real chart dimensions: the empty-state path,
// and the always-rendered legend/notice text that sits outside the chart container itself.
describe("LayerDriftChart", () => {
  it("shows an empty state when there are no layers", () => {
    render(<LayerDriftChart layers={[]} />);
    expect(screen.getByText("No per-layer data for this file.")).toBeInTheDocument();
  });

  it("renders the 4-status legend for non-empty layers", () => {
    const layers: LayerDiff[] = [{ name: "layer1", status: "changed", size: 10 }];
    render(<LayerDriftChart layers={layers} />);
    expect(screen.getByText("Unchanged")).toBeInTheDocument();
    expect(screen.getByText("Changed")).toBeInTheDocument();
    expect(screen.getByText("Added")).toBeInTheDocument();
    expect(screen.getByText("Removed")).toBeInTheDocument();
  });

  it("progressively reveals a large layer set instead of permanently hiding the tail", async () => {
    // v1.3.0 (todo.md item 25): replaces the old hard MAX_RENDERED_LAYERS truncation.
    const layers: LayerDiff[] = Array.from({ length: 4001 }, (_, i) => ({
      name: `layer${i}`,
      status: "unchanged" as const,
      size: 1,
    }));
    render(<LayerDriftChart layers={layers} />);
    expect(screen.getByText(/Rendering \d+ of 4001 layers…/)).toBeInTheDocument();
    // v1.3.4 (Next.js 16 / React 19 upgrade): the default 1000ms waitFor timeout was
    // comfortably enough for jsdom to fire the 5 rAF-driven reveal ticks (500 -> 1500 ->
    // 2500 -> 3500 -> 4001) under React 18 + the old testing-library major -- under
    // React 19's, it now reliably lands on "3500 of 4001" (one tick short) right at the
    // default timeout. Same assertion, same real ticks required, just a longer real-
    // wall-clock allowance for the newer stack's per-tick overhead.
    await waitFor(() => {
      expect(screen.queryByText(/Rendering \d+ of 4001 layers…/)).not.toBeInTheDocument();
    }, { timeout: 5000 });
  });
});
