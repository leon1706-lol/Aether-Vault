// lib/runDetail.ts — pure helpers for the Runs panel's expandable detail view (v1.2.2).
//
// Everything here is client-side composition over data the API already returns
// (GET /api/runs/{id}, GET /api/commits/{hash}) — deliberately NO new server endpoint,
// mirroring how diffWeights.ts builds the Weight Diff view from already-fetched trees.

import type { Commit, Run } from "@/lib/api";

export interface TreeEntryInfo {
  hash: string;
  size?: number | null;
  type?: string;
  layers?: { name: string; hash: string; size?: number }[];
  chunks?: { hash: string; size?: number; offset?: number }[];
}

export type Tree = Record<string, TreeEntryInfo>;

export interface TreeDiffSummary {
  added: string[];
  removed: string[];
  changed: string[];
  chunksReused: number;
  chunksNew: number;
  /** reused / (reused + new); null when neither side has chunked files (no signal ≠ 0). */
  dedupEfficiency: number | null;
  bytesBefore: number;
  bytesAfter: number;
}

/**
 * Walks the parent_run_id chain from `runId` toward the root, SELF FIRST.
 * Cycle-safe (visited set) and tolerant of parents missing from `runs`
 * (they're simply skipped — callers may fetch them separately if needed).
 */
export function lineageChain(runs: Run[], runId: string | null | undefined): Run[] {
  if (!runId) return [];
  const byId = new Map(runs.map((r) => [r.id, r]));
  const chain: Run[] = [];
  const seen = new Set<string>();
  let cursor: string | null | undefined = runId;
  while (cursor && !seen.has(cursor)) {
    seen.add(cursor);
    const run = byId.get(cursor);
    if (!run) break;
    chain.push(run);
    cursor = run.parent_run_id;
  }
  return chain;
}

function _chunkHashes(entry: TreeEntryInfo | undefined): Set<string> {
  return new Set((entry?.chunks ?? []).map((c) => c.hash));
}

function _bytes(tree: Tree): number {
  return Object.values(tree).reduce(
    (sum, e) => sum + (typeof e?.size === "number" ? e.size : 0),
    0,
  );
}

/**
 * Client-side semantic summary between two linked commits' trees — the same questions
 * av's semdiff answers server-side (what moved, how much was reused), composed here so
 * the run detail needs zero extra backend surface.
 */
export function summarizeTreeDiff(
  oldTree: Tree | null | undefined,
  newTree: Tree | null | undefined,
): TreeDiffSummary {
  const oldT = oldTree ?? {};
  const newT = newTree ?? {};
  const added = Object.keys(newT).filter((p) => !(p in oldT)).sort();
  const removed = Object.keys(oldT).filter((p) => !(p in newT)).sort();
  const changed = Object.keys(newT)
    .filter((p) => p in oldT && oldT[p]?.hash !== newT[p]?.hash)
    .sort();

  let chunksReused = 0;
  let chunksNew = 0;
  for (const path of Object.keys(newT)) {
    const chs = _chunkHashes(newT[path]);
    if (chs.size === 0) continue;
    const parentChs = _chunkHashes(oldT[path]);
    for (const h of chs) (parentChs.has(h) ? chunksReused++ : chunksNew++);
  }
  const total = chunksReused + chunksNew;

  return {
    added,
    removed,
    changed,
    chunksReused,
    chunksNew,
    dedupEfficiency: total > 0 ? chunksReused / total : null,
    bytesBefore: _bytes(oldT),
    bytesAfter: _bytes(newT),
  };
}

export interface RunCommitRow {
  hash: string;
  short: string;
  message: string;
  metrics: Record<string, number | string>;
}

// v1.2.5: structural (not nominal) so this accepts both the full `Commit` type AND the
// lighter { hash, message, metrics, timestamp } shape GET /api/runs/{id}/summary returns
// (RunsPanel's primary path since v1.2.5) — one function, two call sites, no duplication.
type MinimalCommit = Pick<Commit, "hash" | "message" | "metrics">;

/** Flattens linked commits into table rows (newest-first as given). */
export function commitMetricsRows(commits: MinimalCommit[]): RunCommitRow[] {
  return commits.map((c) => ({
    hash: c.hash,
    short: c.hash.slice(0, 7),
    message: c.message ?? "",
    metrics: c.metrics ?? {},
  }));
}

/** Union of metric keys across rows, stable order (first-seen). */
export function metricColumns(rows: RunCommitRow[]): string[] {
  const cols: string[] = [];
  for (const r of rows) {
    for (const k of Object.keys(r.metrics)) {
      if (!cols.includes(k)) cols.push(k);
    }
  }
  return cols;
}
