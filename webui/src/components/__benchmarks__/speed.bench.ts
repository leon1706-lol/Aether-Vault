import { bench, describe } from "vitest";

import { buildGraph } from "@/components/CommitGraph";
import { extractMetricKeys } from "@/components/MetricsChart";
import { type Commit } from "@/lib/api";

const SYNTHETIC_COMMIT_COUNT = 200;

function makeSyntheticCommits(count: number): Commit[] {
  const commits: Commit[] = [];
  for (let i = 0; i < count; i++) {
    commits.push({
      hash: `hash_${i}`,
      message: `commit ${i}`,
      author: "bench",
      timestamp: new Date(Date.now() - i * 1000).toISOString(),
      parent_hash: i > 0 ? `hash_${i - 1}` : null,
      root_tree_hash: null,
      tags: [],
      metrics: { accuracy: i / count, loss: 1 - i / count },
    });
  }
  return commits;
}

const commits = makeSyntheticCommits(SYNTHETIC_COMMIT_COUNT);

describe("speed", () => {
  bench(`buildGraph() (${SYNTHETIC_COMMIT_COUNT} commits)`, () => {
    buildGraph(commits);
  });

  bench(`extractMetricKeys() (${SYNTHETIC_COMMIT_COUNT} commits)`, () => {
    extractMetricKeys(commits);
  });
});
