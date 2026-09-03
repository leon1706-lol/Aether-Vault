import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MetricsChart } from "../MetricsChart";
import type { Commit } from "@/lib/api";

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

describe("MetricsChart", () => {
  it("shows a loading state when there are no commits yet", () => {
    render(<MetricsChart commits={[]} loading={true} />);
    expect(screen.getByText("Loading metrics…")).toBeInTheDocument();
  });

  it("shows an empty state when no commit has a numeric metric", () => {
    render(<MetricsChart commits={[makeCommit()]} loading={false} />);
    expect(screen.getByText("No metrics yet")).toBeInTheDocument();
  });

  it("renders a metric count header when commits have numeric metrics", () => {
    const commit = makeCommit({ metrics: { sharpe: 2.5, drawdown: 0.1 } });
    render(<MetricsChart commits={[commit]} loading={false} />);
    expect(screen.getByText("2 metrics")).toBeInTheDocument();
    expect(screen.queryByText("No metrics yet")).not.toBeInTheDocument();
  });

  it("ignores non-numeric metric values when deciding whether metrics exist", () => {
    const commit = makeCommit({ metrics: { note: "not a number" as unknown as number } });
    render(<MetricsChart commits={[commit]} loading={false} />);
    expect(screen.getByText("No metrics yet")).toBeInTheDocument();
  });

  it("shows an error state instead of the empty state when a fetch failed", () => {
    render(<MetricsChart commits={[]} loading={false} error="registry unreachable" />);
    expect(screen.getByText("⚠ registry unreachable")).toBeInTheDocument();
    expect(screen.queryByText("No metrics yet")).not.toBeInTheDocument();
  });

  it("prefers real data over an error when both are present", () => {
    const commit = makeCommit({ metrics: { sharpe: 2.5 } });
    render(<MetricsChart commits={[commit]} loading={false} error="stale error" />);
    expect(screen.getByText("1 metrics")).toBeInTheDocument();
    expect(screen.queryByText("⚠ stale error")).not.toBeInTheDocument();
  });
});
