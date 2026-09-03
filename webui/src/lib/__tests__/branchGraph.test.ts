import { describe, expect, it } from "vitest";

import type { Commit } from "../api";
import { commitsAhead, indexByHash, reachableFromTip } from "../branchGraph";

function commit(hash: string, parentHash: string | null): Commit {
  return {
    hash,
    message: `commit ${hash}`,
    author: "tester",
    timestamp: null,
    parent_hash: parentHash,
    root_tree_hash: null,
    tags: [],
    metrics: {},
  };
}

// A linear chain: c1 <- c2 <- c3 <- c4 (c4 is the tip, c1 the root).
function linearChain(): Commit[] {
  return [
    commit("c1", null),
    commit("c2", "c1"),
    commit("c3", "c2"),
    commit("c4", "c3"),
  ];
}

describe("indexByHash", () => {
  it("maps every commit by its hash", () => {
    const commits = linearChain();
    const map = indexByHash(commits);
    expect(map.size).toBe(4);
    expect(map.get("c3")?.message).toBe("commit c3");
  });

  it("returns an empty map for an empty list", () => {
    expect(indexByHash([]).size).toBe(0);
  });
});

describe("reachableFromTip", () => {
  it("walks the full parent chain when every commit is loaded", () => {
    const commitByHash = indexByHash(linearChain());
    const { hashes, truncated } = reachableFromTip("c4", commitByHash);
    expect(hashes).toEqual(new Set(["c4", "c3", "c2", "c1"]));
    expect(truncated).toBe(false);
  });

  it("stops at the root (parent_hash null) without truncation", () => {
    const commitByHash = indexByHash(linearChain());
    const { hashes, truncated } = reachableFromTip("c1", commitByHash);
    expect(hashes).toEqual(new Set(["c1"]));
    expect(truncated).toBe(false);
  });

  it("reports truncated when the walk runs off the edge of the loaded window", () => {
    // Only c3 and c4 are loaded — c3's parent (c2) isn't in the map.
    const commitByHash = indexByHash([commit("c3", "c2"), commit("c4", "c3")]);
    const { hashes, truncated } = reachableFromTip("c4", commitByHash);
    expect(hashes).toEqual(new Set(["c4", "c3"]));
    expect(truncated).toBe(true);
  });

  it("returns an empty, non-truncated result for an undefined tip", () => {
    const { hashes, truncated } = reachableFromTip(undefined, new Map());
    expect(hashes.size).toBe(0);
    expect(truncated).toBe(false);
  });

  it("guards against a cycle instead of looping forever", () => {
    // c1 -> c2 -> c1 (a corrupted/cyclic parent chain).
    const commitByHash = indexByHash([commit("c1", "c2"), commit("c2", "c1")]);
    const { hashes } = reachableFromTip("c1", commitByHash);
    expect(hashes).toEqual(new Set(["c1", "c2"]));
  });
});

describe("commitsAhead", () => {
  it("counts commits on tip not reachable from base", () => {
    // main: c1 <- c2 (tip). feature branches off c2: c1 <- c2 <- c3 <- c4 (tip).
    const commitByHash = indexByHash(linearChain());
    const { count, truncated } = commitsAhead("c4", "c2", commitByHash);
    expect(count).toBe(2); // c3, c4
    expect(truncated).toBe(false);
  });

  it("is zero when tip equals base", () => {
    const commitByHash = indexByHash(linearChain());
    const { count } = commitsAhead("c2", "c2", commitByHash);
    expect(count).toBe(0);
  });

  it("is zero when tip is an ancestor of base (behind, not ahead)", () => {
    const commitByHash = indexByHash(linearChain());
    const { count } = commitsAhead("c1", "c4", commitByHash);
    expect(count).toBe(0);
  });

  it("reports truncated when tip's history runs off the loaded window before joining base", () => {
    // Only c3/c4 loaded; base "c1" isn't in the map at all, so the walk from c4 can
    // never join it before running out of loaded history.
    const commitByHash = indexByHash([commit("c3", "c2"), commit("c4", "c3")]);
    const { truncated } = commitsAhead("c4", "c1", commitByHash);
    expect(truncated).toBe(true);
  });

  it("handles an undefined base tip (no base branch) as everything-ahead", () => {
    const commitByHash = indexByHash(linearChain());
    const { count } = commitsAhead("c4", undefined, commitByHash);
    expect(count).toBe(4);
  });
});
