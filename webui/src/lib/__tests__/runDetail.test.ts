import { describe, expect, it } from "vitest";

import {
  commitMetricsRows,
  lineageChain,
  metricColumns,
  summarizeTreeDiff,
} from "../runDetail";
import type { Commit, Run } from "../../lib/api";

function run(id: string, parentRunId: string | null = null): Run {
  return {
    id,
    project_id: "p",
    name: id.slice(0, 4),
    status: "completed",
    parent_run_id: parentRunId,
    created_by: "tester",
    metrics_summary: {},
    created_at: null,
    completed_at: null,
  };
}

describe("lineageChain", () => {
  it("walks self â†’ root through parent_run_id links", () => {
    const runs = [run("child", "mid"), run("mid", "root"), run("root", null)];
    const chain = lineageChain(runs, "child");
    expect(chain.map((r) => r.id)).toEqual(["child", "mid", "root"]);
  });

  it("is cycle-safe and stops at missing parents", () => {
    // a â†” b cycle plus a dangling parent that isn't in the list
    const runs = [run("a", "b"), run("b", "a"), run("c", "ghost")];
    expect(lineageChain(runs, "a").map((r) => r.id)).toEqual(["a", "b"]);
    expect(lineageChain(runs, "c").map((r) => r.id)).toEqual(["c"]);
  });

  it("returns empty for a null/unknown id", () => {
    expect(lineageChain([run("a")], null)).toEqual([]);
    expect(lineageChain([], "nope")).toEqual([]);
  });
});

describe("summarizeTreeDiff", () => {
  const e = (hash: string, size = 10) => ({ hash, size });

  it("classifies added/removed/changed and byte totals", () => {
    const oldTree = { kept: e("k"), gone: e("g") };
    const newTree = { kept: e("k"), fresh: e("f") };
    const s = summarizeTreeDiff(oldTree, newTree);
    expect(s.added).toEqual(["fresh"]);
    expect(s.removed).toEqual(["gone"]);
    expect(s.changed).toEqual([]);
    expect(s.bytesBefore).toBe(20);
    expect(s.bytesAfter).toBe(20);
  });

  it("computes chunk reuse + dedup efficiency; null when no chunks", () => {
    const chunksA = [{ hash: "1" }, { hash: "2" }, { hash: "3" }, { hash: "4" }];
    const chunksB = [{ hash: "1" }, { hash: "2" }, { hash: "9" }];
    const oldTree = { "ckpt.pt": { hash: "a", size: 0, chunks: chunksA } };
    const newTree = { "ckpt.pt": { hash: "b", size: 0, chunks: chunksB } };
    const s = summarizeTreeDiff(oldTree, newTree);
    expect(s.chunksReused).toBe(2);
    expect(s.chunksNew).toBe(1);
    expect(s.dedupEfficiency).toBeCloseTo(2 / 3);

    const noChunks = summarizeTreeDiff({ a: e("x") }, { a: e("y") });
    expect(noChunks.dedupEfficiency).toBeNull();
    expect(summarizeTreeDiff(null, null).dedupEfficiency).toBeNull();
  });

  it("treats identical chunk populations as fully reused (1.0)", () => {
    const chunks = [{ hash: "same" }];
    const s = summarizeTreeDiff(
      { "m.pt": { hash: "a", size: 0, chunks } },
      { "m.pt": { hash: "b", size: 0, chunks: [...chunks] } },
    );
    expect(s.dedupEfficiency).toBe(1);
    expect(s.changed).toContain("m.pt"); // whole-file hash moved even though chunks didn't
  });
});

describe("commitMetricsRows / metricColumns", () => {
  it("flattens rows and unions metric keys in first-seen order", () => {
    const commits = [
      { hash: "a".repeat(64), message: "first", metrics: { loss: 0.5 } },
      { hash: "b".repeat(64), message: "", metrics: { loss: 0.4, steps: 100 } },
    ] as unknown as Commit[];
    const rows = commitMetricsRows(commits);
    expect(rows.map((r) => r.short)).toEqual(["aaaaaaa", "bbbbbbb"]);
    expect(metricColumns(rows)).toEqual(["loss", "steps"]);
  });
});
