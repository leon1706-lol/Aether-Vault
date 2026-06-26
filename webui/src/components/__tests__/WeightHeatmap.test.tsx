import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { WeightHeatmap } from "../WeightHeatmap";
import type { LayerDiff } from "@/lib/diffWeights";

describe("WeightHeatmap", () => {
  it("shows an empty state when there are no layers", () => {
    render(<WeightHeatmap layers={[]} />);
    expect(screen.getByText("No per-layer data for this file.")).toBeInTheDocument();
  });

  it("renders one cell per layer with a status-specific class", () => {
    const layers: LayerDiff[] = [
      { name: "layer1", status: "unchanged", size: 10 },
      { name: "layer2", status: "changed", size: 20 },
    ];
    const { container } = render(<WeightHeatmap layers={layers} />);
    const cells = container.querySelectorAll(".layer-cell");
    expect(cells).toHaveLength(2);
    expect(cells[0]).toHaveClass("layer-cell--unchanged");
    expect(cells[1]).toHaveClass("layer-cell--changed");
  });

  it("truncates and shows a notice past MAX_RENDERED_LAYERS", () => {
    const layers: LayerDiff[] = Array.from({ length: 4001 }, (_, i) => ({
      name: `layer${i}`,
      status: "unchanged" as const,
      size: 1,
    }));
    render(<WeightHeatmap layers={layers} />);
    expect(screen.getByText("Showing 4000 of 4001 layers")).toBeInTheDocument();
  });
});
