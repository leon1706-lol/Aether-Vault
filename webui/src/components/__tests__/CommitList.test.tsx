import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CommitList } from "../CommitList";
import type { Commit } from "@/lib/api";

function makeCommit(overrides: Partial<Commit> = {}): Commit {
  return {
    hash: "a".repeat(64),
    message: "first commit",
    author: "tester",
    timestamp: new Date().toISOString(),
    parent_hash: null,
    root_tree_hash: "b".repeat(64),
    tags: [],
    metrics: {},
    ...overrides,
  };
}

describe("CommitList", () => {
  it("shows a loading state when there are no commits yet", () => {
    render(<CommitList commits={[]} loading={true} />);
    expect(screen.getByText("Loading commits…")).toBeInTheDocument();
  });

  it("shows an empty state when loading is done and there are no commits", () => {
    render(<CommitList commits={[]} loading={false} />);
    expect(screen.getByText("No commits yet")).toBeInTheDocument();
  });

  it("renders the commit message, tags, and metrics", () => {
    const commit = makeCommit({ tags: ["v1"], metrics: { sharpe: 2.5 } });
    render(<CommitList commits={[commit]} loading={false} />);
    expect(screen.getByText("first commit")).toBeInTheDocument();
    expect(screen.getByText("🏷 v1")).toBeInTheDocument();
    expect(screen.getByText("sharpe=2.50")).toBeInTheDocument();
  });

  it("shows 'just now' for a commit timestamped this instant", () => {
    render(<CommitList commits={[makeCommit()]} loading={false} />);
    expect(screen.getByText("just now")).toBeInTheDocument();
  });
});
