// branchGraph.ts — client-side helpers for partitioning the already-loaded commit window by
// branch reachability. There is no server-side "commits on branch X" endpoint, so every panel
// that needs per-branch filtering (Commits, Branches, Metrics) walks parent_hash locally from
// each ref's tip. This only sees whatever commits are already loaded (e.g. the last N from
// fetchCommits) — if a branch's history extends further back than the loaded window, the walk
// silently stops at the edge of what's available. Callers must surface that as an
// "ahead of loaded history" caveat rather than presenting it as an exact count.
import type { Commit } from "./api";

export function indexByHash(commits: Commit[]): Map<string, Commit> {
  const map = new Map<string, Commit>();
  for (const c of commits) map.set(c.hash, c);
  return map;
}

// Walks parent_hash from `tipHash` until it hits a commit not present in `commitByHash`
// (the edge of the loaded window) or a hash already visited (defensive cycle guard).
// Returns the set of reachable hashes plus whether the walk was cut short by that edge.
export function reachableFromTip(
  tipHash: string | undefined,
  commitByHash: Map<string, Commit>
): { hashes: Set<string>; truncated: boolean } {
  const hashes = new Set<string>();
  let cur = tipHash;
  while (cur && !hashes.has(cur)) {
    const commit = commitByHash.get(cur);
    if (!commit) {
      return { hashes, truncated: true };
    }
    hashes.add(cur);
    cur = commit.parent_hash ?? undefined;
  }
  return { hashes, truncated: false };
}

// Number of commits reachable from `tipHash` that are NOT reachable from `baseTipHash`
// (e.g. "commits ahead of main"). `truncated` is true if the walk ran off the edge of the
// loaded commit window before it could either find a common ancestor or exhaust the branch's
// own history — callers should label the count as a lower bound in that case.
export function commitsAhead(
  tipHash: string | undefined,
  baseTipHash: string | undefined,
  commitByHash: Map<string, Commit>
): { count: number; truncated: boolean } {
  const base = reachableFromTip(baseTipHash, commitByHash);
  let count = 0;
  let cur = tipHash;
  const visited = new Set<string>();
  while (cur && !base.hashes.has(cur) && !visited.has(cur)) {
    const commit = commitByHash.get(cur);
    if (!commit) {
      return { count, truncated: true };
    }
    visited.add(cur);
    count++;
    cur = commit.parent_hash ?? undefined;
  }
  // Ran out of loaded history without ever joining base's reachable set.
  if (cur && !base.hashes.has(cur)) {
    return { count, truncated: true };
  }
  return { count, truncated: base.truncated };
}
