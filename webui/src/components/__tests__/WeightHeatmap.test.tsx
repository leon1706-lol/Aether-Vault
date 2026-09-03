import { render, screen, waitFor } from "@testing-library/react";
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

  it("progressively reveals a large layer set instead of permanently hiding the tail", async () => {
    // v1.3.0 (todo.md item 25): replaces the old hard MAX_RENDERED_LAYERS truncation —
    // every layer eventually renders, just spread over a couple of animation frames.
    // 1200 (not a much larger number) keeps this test's real jsdom node count — and
    // therefore its wall-clock render cost per frame — small enough to resolve within
    // waitFor's default timeout even on a slow CI/dev machine; the contract being
    // proven (progressive reveal completes, nothing stays permanently hidden) doesn't
    // need thousands of frames to demonstrate.
    const layers: LayerDiff[] = Array.from({ length: 1200 }, (_, i) => ({
      name: `layer${i}`,
      status: "unchanged" as const,
      size: 1,
    }));
    const { container } = render(<WeightHeatmap layers={layers} />);

    // First paint: only an initial batch is present, with a progress notice.
    expect(container.querySelectorAll(".layer-cell").length).toBeLessThan(1200);
    expect(screen.getByText(/Rendering \d+ of 1200 layers…/)).toBeInTheDocument();

    // Eventually every layer renders and the progress notice disappears — nothing is
    // permanently hidden the way the old hard cutoff left it.
    await waitFor(
      () => {
        expect(container.querySelectorAll(".layer-cell")).toHaveLength(1200);
      },
      { timeout: 5000 }
    );
    expect(screen.queryByText(/Rendering \d+ of 1200 layers…/)).not.toBeInTheDocument();
  });

  it("renders small layer sets in full on the very first paint (no visible progress notice)", () => {
    const layers: LayerDiff[] = Array.from({ length: 3 }, (_, i) => ({
      name: `layer${i}`,
      status: "unchanged" as const,
      size: 1,
    }));
    const { container } = render(<WeightHeatmap layers={layers} />);
    expect(container.querySelectorAll(".layer-cell")).toHaveLength(3);
    expect(screen.queryByText(/Rendering \d+ of 3 layers…/)).not.toBeInTheDocument();
  });
});
