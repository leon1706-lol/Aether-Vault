import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { BranchesPanel } from "../BranchesPanel";
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

describe("BranchesPanel", () => {
  it("shows a loading state when there are no refs yet", () => {
    render(<BranchesPanel refs={{}} commits={[]} loading={true} />);
    expect(screen.getByText("Loading branches…")).toBeInTheDocument();
  });

  it("shows an empty state when loading is done and there are no branches", () => {
    render(<BranchesPanel refs={{}} commits={[]} loading={false} />);
    expect(screen.getByText("No branches found")).toBeInTheDocument();
  });

  it("computes an ahead-of-main count for a feature branch sharing the same loaded history", () => {
    const mainTip = makeCommit({ hash: "a".repeat(64), parent_hash: null });
    const featureC2 = makeCommit({ hash: "c".repeat(64), parent_hash: mainTip.hash });
    const featureC3 = makeCommit({ hash: "d".repeat(64), parent_hash: featureC2.hash });

    render(
      <BranchesPanel
        refs={{ "proj/main": mainTip.hash, "proj/feature": featureC3.hash }}
        commits={[featureC3, featureC2, mainTip]}
        loading={false}
      />
    );

    expect(screen.getByText(/2 ahead of proj\/main/)).toBeInTheDocument();
  });

  it("expanding a branch row lists its reachable commits", async () => {
    const user = userEvent.setup();
    const tip = makeCommit({ hash: "a".repeat(64), message: "only commit on main" });

    render(<BranchesPanel refs={{ main: tip.hash }} commits={[tip]} loading={false} />);
    await user.click(screen.getByText("main"));

    // The tip's message already appears once as a preview under the branch header — expanding
    // shows it again inside the reachable-commits list, so two matches is the expected outcome.
    expect(screen.getAllByText("only commit on main")).toHaveLength(2);
  });

  it("notes that branch delete is not available", () => {
    const tip = makeCommit();
    render(<BranchesPanel refs={{ main: tip.hash }} commits={[tip]} loading={false} />);
    expect(screen.getByText(/Branch delete isn't available yet/)).toBeInTheDocument();
  });
});
