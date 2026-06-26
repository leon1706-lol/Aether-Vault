import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BranchList } from "../BranchList";
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

describe("BranchList", () => {
  it("shows a loading state when there are no refs yet", () => {
    render(<BranchList refs={{}} commits={[]} loading={true} />);
    expect(screen.getByText("Loading branches…")).toBeInTheDocument();
  });

  it("shows an empty state when loading is done and there are no branches", () => {
    render(<BranchList refs={{}} commits={[]} loading={false} />);
    expect(screen.getByText("No branches found")).toBeInTheDocument();
  });

  it("strips a single project_id/ prefix from the displayed branch name", () => {
    const commit = makeCommit();
    render(
      <BranchList
        refs={{ "proj-a/main": commit.hash }}
        commits={[commit]}
        loading={false}
      />
    );
    expect(screen.getByText("main")).toBeInTheDocument();
    expect(screen.queryByText("proj-a/main")).not.toBeInTheDocument();
  });

  it("prefixes with the project name when the tip commit has one", () => {
    const commit = makeCommit({ project_name: "my-llm" });
    render(
      <BranchList
        refs={{ "proj-a/main": commit.hash }}
        commits={[commit]}
        loading={false}
      />
    );
    expect(screen.getByText("my-llm / main")).toBeInTheDocument();
  });
});
