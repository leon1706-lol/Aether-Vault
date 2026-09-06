import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LayerDriftChart } from "../LayerDriftChart";
import type { LayerDiff } from "@/lib/diffWeights";

// recharts' <ResponsiveContainer> resolves to a 0x0 box under jsdom, so these tests stick
// to what's verifiable without real chart dimensions: the empty-state path and the
// always-rendered legend/notice text.
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
    const layers: LayerDiff[] = Array.from({ length: 4001 }, (_, i) => ({
      name: `layer${i}`,
      status: "unchanged" as const,
      size: 1,
    }));
    render(<LayerDriftChart layers={layers} />);
    expect(screen.getByText(/Rendering \d+ of 4001 layers…/)).toBeInTheDocument();
    // A longer-than-default timeout: React 19's per-tick overhead can need more than
    // 1000ms for jsdom to fire all 5 rAF-driven reveal ticks.
    await waitFor(() => {
      expect(screen.queryByText(/Rendering \d+ of 4001 layers…/)).not.toBeInTheDocument();
    }, { timeout: 5000 });
  });
});
