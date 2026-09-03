import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

// buildGraph is a pure function (no hooks/DOM) — importing it from the component module
// is safe; only the React render path needs jsdom.
import { buildGraph, CommitGraph } from "../CommitGraph";
import type { Commit } from "../../lib/api";

const H = (seed: string) => seed.padEnd(64, "0").slice(0, 64);

function commit(partial: Partial<Commit> & { hash: string }): Commit {
  return {
    message: partial.hash.slice(0, 7),
    author: "tester",
    timestamp: null,
    parent_hash: null,
    root_tree_hash: null,
    tags: [],
    metrics: {},
    ...partial,
  };
}

describe("buildGraph — merge visualization", () => {
  it("draws TWO edges for a merge commit with parents[]", () => {
    const main1 = H("main1");
    const feat1 = H("feat1");
    const merge = H("merge");

    const commits = [
      // Newest first as the server returns them; timestamps equal so order is preserved.
      commit({ hash: merge, parents: [main1, feat1], parent_hash: main1 }),
      commit({ hash: main1, parents: [] }),
      commit({ hash: feat1, parents: [] }),
    ];

    const { edges } = buildGraph(commits);
    expect(edges).toHaveLength(2);

    // Both parents connected: merge → main1 and merge → feat1.
    const targets = new Set(edges.map((e) => e.y2));
    expect(targets.size).toBe(2);
  });

  it("keeps single-parent commits at exactly one edge", () => {
    const p = H("parent");
    const c = H("child");
    const { edges } = buildGraph([
      commit({ hash: c, parent_hash: p, parents: [p] }),
      commit({ hash: p, parents: [] }),
    ]);
    expect(edges).toHaveLength(1);
  });

  it("falls back to parent_hash when parents[] is absent (older payloads)", () => {
    const p = H("parent");
    const c = H("child");
    const { edges } = buildGraph([
      commit({ hash: c, parent_hash: p }), // no parents field at all
      commit({ hash: p }),
    ]);
    expect(edges).toHaveLength(1);
  });

  it("inherits lanes through the FIRST parent only", () => {
    const base = H("base");
    const side = H("side");
    const main = H("main");
    const merge = H("merge");

    // Order as the server sends it: newest first. `merge` sits on lane 0; its first
    // parent `main` must inherit lane 0, while the second parent `side` keeps its own
    // distinct lane instead of stealing the stream.
    const { nodes } = buildGraph([
      commit({ hash: merge, parents: [main, side], parent_hash: main }),
      commit({ hash: main, parents: [base] }),
      commit({ hash: side, parents: [] }),
      commit({ hash: base, parents: [] }),
    ]);

    const colOf = Object.fromEntries(nodes.map((n) => [n.commit.hash, n.col]));
    expect(colOf[merge]).toBe(colOf[main]);
    expect(colOf[side]).not.toBe(colOf[main]);
  });

  it("silently drops edges to parents outside the loaded window", () => {
    const c = H("child");
    const { edges } = buildGraph([commit({ hash: c, parents: [H("missing")] })]);
    expect(edges).toHaveLength(0);
  });

  it("renders an octopus merge with three parents as three edges", () => {
    const a = H("a");
    const b = H("b");
    const c = H("c");
    const m = H("octopus");
    const { edges } = buildGraph([
      commit({ hash: m, parents: [a, b, c], parent_hash: a }),
      commit({ hash: a }),
      commit({ hash: b }),
      commit({ hash: c }),
    ]);
    expect(edges).toHaveLength(3);
  });
});

describe("CommitGraph — error state", () => {
  it("shows an error state instead of the empty state when a fetch failed", () => {
    render(<CommitGraph commits={[]} loading={false} error="registry unreachable" />);
    expect(screen.getByText("⚠ registry unreachable")).toBeInTheDocument();
    expect(screen.queryByText("No commits yet")).not.toBeInTheDocument();
  });

  it("prefers real data over an error when both are present", () => {
    const c = commit({ hash: H("c1"), parents: [] });
    render(<CommitGraph commits={[c]} loading={false} error="stale error" />);
    expect(screen.queryByText("⚠ stale error")).not.toBeInTheDocument();
  });
});
