import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { WeightDiffPanel } from "../WeightDiffPanel";
import type { Commit } from "@/lib/api";

const { fetchCommitsWithLayers } = vi.hoisted(() => ({
  fetchCommitsWithLayers: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchCommitsWithLayers };
});

function makeCommit(overrides: Partial<Commit> = {}): Commit {
  return {
    hash: "a".repeat(64),
    message: "checkpoint",
    author: "tester",
    timestamp: "2026-06-26T00:00:00Z",
    parent_hash: null,
    root_tree_hash: "b".repeat(64),
    tags: [],
    metrics: {},
    ...overrides,
  };
}

const commitV1 = makeCommit({
  hash: "a".repeat(64),
  timestamp: "2026-06-26T00:00:00Z",
  tree: {
    "model.safetensors": {
      hash: "t1",
      size: 200,
      type: "artifact",
      layers: [
        { name: "layer0", hash: "L0", size: 100 },
        { name: "layer1", hash: "L1", size: 100 },
      ],
    },
  },
});

const commitV2 = makeCommit({
  hash: "c".repeat(64),
  timestamp: "2026-06-27T00:00:00Z",
  parent_hash: commitV1.hash,
  tree: {
    "model.safetensors": {
      hash: "t2",
      size: 200,
      type: "artifact",
      layers: [
        { name: "layer0", hash: "L0", size: 100 }, // unchanged
        { name: "layer1", hash: "L1-changed", size: 100 }, // changed
      ],
    },
  },
});

describe("WeightDiffPanel", () => {
  it("shows a loading state while checkpoints are being resolved", () => {
    fetchCommitsWithLayers.mockReturnValue(new Promise(() => {})); // never resolves
    render(<WeightDiffPanel />);
    expect(screen.getByText("Loading checkpoints…")).toBeInTheDocument();
  });

  it("shows an empty state when there are no model checkpoints", async () => {
    fetchCommitsWithLayers.mockResolvedValueOnce([]);
    render(<WeightDiffPanel />);
    await waitFor(() => expect(screen.getByText("No model checkpoints found")).toBeInTheDocument());
  });

  it("computes and renders a per-layer diff once two checkpoints are selected", async () => {
    const user = userEvent.setup();
    fetchCommitsWithLayers.mockResolvedValueOnce([commitV1, commitV2]);

    render(<WeightDiffPanel />);

    await waitFor(() => expect(screen.getByText("v1")).toBeInTheDocument());
    await user.click(screen.getByText("v1"));
    await user.click(screen.getByText("v2"));

    await waitFor(() => expect(screen.getByText("Total Layers")).toBeInTheDocument());
    const totalLayersCard = screen.getByText("Total Layers").closest(".stat-card")!;
    expect(within(totalLayersCard).getByText("2")).toBeInTheDocument();

    // "Changed" also appears as a status-legend label in the LayerDriftChart rendered below
    // the stat cards — scope to the stat-label specifically to disambiguate.
    const changedLabel = screen
      .getAllByText("Changed")
      .find((el) => el.className === "stat-label")!;
    const changedCard = changedLabel.closest(".stat-card")!;
    expect(within(changedCard).getByText("1")).toBeInTheDocument();

    const pctCard = screen.getByText("% Changed").closest(".stat-card")!;
    expect(within(pctCard).getByText("50%")).toBeInTheDocument();
  });

  it("warns when the two selected slots are different files", async () => {
    const user = userEvent.setup();
    const otherFileCommit = makeCommit({
      hash: "d".repeat(64),
      timestamp: "2026-06-28T00:00:00Z",
      tree: {
        "other.safetensors": {
          hash: "t3",
          size: 100,
          type: "artifact",
          layers: [{ name: "layer0", hash: "X0", size: 100 }],
        },
      },
    });
    fetchCommitsWithLayers.mockResolvedValueOnce([commitV1, otherFileCommit]);

    render(<WeightDiffPanel />);

    await waitFor(() => expect(screen.getByText("v1")).toBeInTheDocument());
    await user.click(screen.getByText("v1"));
    await user.click(screen.getByText("v2"));

    await waitFor(() =>
      expect(screen.getByText(/Comparing different files/)).toBeInTheDocument()
    );
  });
});
