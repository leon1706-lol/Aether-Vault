import { render, screen } from "@testing-library/react";
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

  it("truncates and shows a notice past MAX_RENDERED_LAYERS", () => {
    const layers: LayerDiff[] = Array.from({ length: 4001 }, (_, i) => ({
      name: `layer${i}`,
      status: "unchanged" as const,
      size: 1,
    }));
    render(<LayerDriftChart layers={layers} />);
    expect(screen.getByText("Showing 4000 of 4001 layers")).toBeInTheDocument();
  });
});
