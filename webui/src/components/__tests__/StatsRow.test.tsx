import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatsRow } from "../StatsRow";
import type { DashboardData } from "@/lib/api";

function makeData(overrides: Partial<DashboardData> = {}): DashboardData {
  return {
    health: { status: "ok", version: "1.4.0" },
    refs: { main: "a".repeat(64) },
    commits: [
      {
        hash: "a".repeat(64),
        message: "first",
        author: "tester",
        timestamp: "2026-06-26T00:00:00Z",
        parent_hash: null,
        root_tree_hash: "b".repeat(64),
        tags: ["v1"],
        metrics: { sharpe: 2.5 },
      },
    ],
    stats: { total_objects: 3, total_size_bytes: 2048 },
    error: null,
    ...overrides,
  };
}

describe("StatsRow", () => {
  it("shows placeholder dashes while loading", () => {
    render(<StatsRow data={null} loading={true} />);
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("renders real counts once data has loaded", () => {
    render(<StatsRow data={makeData()} loading={false} />);

    // "1" appears in both the Total Commits and Branches cards (1 commit, 1 branch) — scope
    // each assertion to its own card rather than asserting the ambiguous text globally.
    const commitsCard = screen.getByText("Total Commits").closest(".stat-card")!;
    expect(within(commitsCard).getByText("1")).toBeInTheDocument();

    const objectsCard = screen.getByText("CAS Objects").closest(".stat-card")!;
    expect(within(objectsCard).getByText("3")).toBeInTheDocument();
    expect(within(objectsCard).getByText("2 KB")).toBeInTheDocument(); // formatBytes(2048)

    expect(screen.getByText("Branches")).toBeInTheDocument();
  });

  it("surfaces the first numeric metric as its own card", () => {
    render(<StatsRow data={makeData()} loading={false} />);
    expect(screen.getByText("SHARPE")).toBeInTheDocument();
    expect(screen.getByText("2.500")).toBeInTheDocument();
  });

  it("omits the metric card when no commit has a numeric metric", () => {
    render(<StatsRow data={makeData({ commits: [] })} loading={false} />);
    expect(screen.queryByText("SHARPE")).not.toBeInTheDocument();
  });
});
