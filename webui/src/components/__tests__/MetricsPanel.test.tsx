import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { MetricsPanel } from "../MetricsPanel";
import type { Commit, Ref } from "@/lib/api";

function makeCommit(overrides: Partial<Commit> = {}): Commit {
  return {
    hash: "a".repeat(64),
    message: "first commit",
    author: "tester",
    timestamp: "2026-06-26T00:00:00Z",
    parent_hash: null,
    root_tree_hash: "b".repeat(64),
    tags: [],
    metrics: {},
    ...overrides,
  };
}

describe("MetricsPanel", () => {
  it("shows a loading state when there are no commits yet", () => {
    render(<MetricsPanel commits={[]} refs={{}} loading={true} />);
    expect(screen.getByText("Loading metrics…")).toBeInTheDocument();
  });

  it("shows an empty state when no commit has a numeric metric", () => {
    render(<MetricsPanel commits={[makeCommit()]} refs={{}} loading={false} />);
    expect(screen.getByText("No metrics yet")).toBeInTheDocument();
  });

  it("renders the metrics table with one row per commit carrying metrics", () => {
    const commits = [
      makeCommit({ hash: "a".repeat(64), metrics: { sharpe: 2.5 } }),
      makeCommit({ hash: "c".repeat(64), metrics: { sharpe: 3.1 } }),
    ];
    render(<MetricsPanel commits={commits} refs={{ main: "a".repeat(64) }} loading={false} />);

    expect(screen.getByText("Metrics Table")).toBeInTheDocument();
    expect(screen.getByText("2 commits")).toBeInTheDocument();
    expect(screen.getByText("2.500")).toBeInTheDocument();
    expect(screen.getByText("3.100")).toBeInTheDocument();
  });

  it("toggling a metric chip dims it without removing the table row", async () => {
    const user = userEvent.setup();
    const commits = [makeCommit({ metrics: { sharpe: 2.5 } })];
    render(<MetricsPanel commits={commits} refs={{}} loading={false} />);

    const chip = screen.getByRole("button", { name: "sharpe" });
    await user.click(chip);
    expect(chip).toHaveStyle({ opacity: "0.4" });
    // Table row for the metric is unaffected by the chart-visibility toggle.
    expect(screen.getByText("2.500")).toBeInTheDocument();
  });

  it("scopes commits to the selected branch", async () => {
    const user = userEvent.setup();
    const tip: Commit = makeCommit({ hash: "a".repeat(64), parent_hash: null, metrics: { sharpe: 1 } });
    const other: Commit = makeCommit({ hash: "c".repeat(64), parent_hash: null, metrics: { sharpe: 9 } });
    const refs: Ref = { main: tip.hash };

    render(<MetricsPanel commits={[tip, other]} refs={refs} loading={false} />);
    expect(screen.getByText("2 commits")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Branch"), "main");
    expect(screen.getByText("1 commits")).toBeInTheDocument();
  });

  it("shows an error state instead of the empty state when a fetch failed", () => {
    render(<MetricsPanel commits={[]} refs={{}} loading={false} error="registry unreachable" />);
    expect(screen.getByText("⚠ registry unreachable")).toBeInTheDocument();
    expect(screen.queryByText("No metrics yet")).not.toBeInTheDocument();
  });

  it("prefers real data over an error when both are present", () => {
    const commits = [makeCommit({ metrics: { sharpe: 2.5 } })];
    render(<MetricsPanel commits={commits} refs={{}} loading={false} error="stale error" />);
    expect(screen.getByText("Metrics Table")).toBeInTheDocument();
    expect(screen.queryByText("⚠ stale error")).not.toBeInTheDocument();
  });
});
