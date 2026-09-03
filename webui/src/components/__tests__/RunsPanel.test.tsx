import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { RunsPanel } from "../RunsPanel";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchRuns: vi.fn(),
  fetchLatestEventId: vi.fn(),
  fetchRunSummary: vi.fn(),
  fetchRunMetrics: vi.fn(),
  // MetricsChart.tsx (rendered internally once there's enough run history) imports
  // shortHash directly from this module — a pure function, safe to reimplement here
  // rather than importOriginal-ing the whole module just for one helper.
  shortHash: (hash: string) => hash.slice(0, 7),
}));

const mocked = vi.mocked(api);

beforeEach(() => {
  // v1.2.5 deep linking touches window.history — keep each test's URL state isolated.
  window.history.replaceState(null, "", "/");
  // v1.3.0: call-count assertions (e.g. "never calls fetchRunMetrics") need a clean
  // slate — mockResolvedValue alone doesn't clear .mock.calls from earlier tests.
  mocked.fetchRunMetrics.mockReset();
});

function baseRun(overrides: Partial<api.Run> = {}): api.Run {
  return {
    id: "abcdef1234567890abcdef1234567890",
    project_id: "p",
    name: null,
    status: "running",
    parent_run_id: null,
    created_by: null,
    metrics_summary: {},
    created_at: null,
    completed_at: null,
    ...overrides,
  };
}

describe("RunsPanel", () => {
  it("renders an empty state when the registry has no runs", async () => {
    mocked.fetchRuns.mockResolvedValue([]);
    mocked.fetchLatestEventId.mockResolvedValue(0);
    render(<RunsPanel projectId={null} />);
    await waitFor(() => {
      expect(screen.getByText(/No runs yet/i)).toBeInTheDocument();
    });
  });

  it("lists runs with status colors and metric summaries", async () => {
    mocked.fetchRuns.mockResolvedValue([
      {
        id: "abcdef1234567890abcdef1234567890",
        project_id: "p",
        name: "fine-tune v2",
        status: "completed",
        parent_run_id: null,
        created_by: "alice",
        metrics_summary: { val_loss: 0.31, steps: 12000 },
        created_at: "2026-08-24T10:00:00",
        completed_at: null,
      },
      baseRun({
        id: "bbbb1111222233334444555566667777",
        created_by: "agent-7",
      }),
    ]);
    let eventId = 41; // climbs across polls so the live-badge path is exercised
    mocked.fetchLatestEventId.mockImplementation(async () => ++eventId);
    render(<RunsPanel projectId="p" runsPollMs={40} eventsPollMs={40} />);

    await waitFor(() => expect(screen.getByText("● completed")).toBeInTheDocument());
    expect(screen.getByText("fine-tune v2")).toBeInTheDocument();
    expect(screen.getByText("val_loss=")).toBeInTheDocument();
    expect(screen.getByText("0.31")).toBeInTheDocument();
    expect(screen.getByText("● running")).toBeInTheDocument();
    // Live badge appears once a newer event id is observed (polls every 40ms here):
    await waitFor(
      () => expect(screen.getByTitle(/New activity/i)).toBeInTheDocument(),
      { timeout: 3000 },
    );
  });

  it("surfaces registry errors instead of pretending everything is fine", async () => {
    mocked.fetchRuns.mockRejectedValue(new Error("HTTP 503"));
    render(<RunsPanel projectId={null} />);
    await waitFor(() => expect(screen.getByText(/HTTP 503/)).toBeInTheDocument());
  });
});

// ---------------------------------------------------------------------------
// v1.2.5 run detail: ONE request (GET /api/runs/{id}/summary) instead of the old
// fetchRun()+N×fetchCommit() fan-out; dedicated panel below the table; deep linking.
// ---------------------------------------------------------------------------

describe("RunsPanel detail", () => {
  const childRun = baseRun({
    id: "child000000000000000000000000001",
    name: "fine-tune",
    parent_run_id: "parent0000000000000000000000001",
    commit_hashes: ["c" + "0".repeat(63), "c" + "1".repeat(63)],
    metrics_summary: { loss: 0.2 },
  });

  function baseSummary(overrides: Partial<api.RunSummary> = {}): api.RunSummary {
    return {
      run: childRun,
      lineage: [
        { id: childRun.id, name: "fine-tune", status: "running" },
        { id: "parent0000000000000000000000001", name: "baseline", status: "completed" },
      ],
      commits: [
        { hash: "c" + "0".repeat(63), message: "latest work", metrics: { loss: 0.2 },
          timestamp: "2026-08-25T02:00:00" },
        { hash: "c" + "1".repeat(63), message: "earlier work", metrics: { loss: 0.5 },
          timestamp: "2026-08-25T01:00:00" },
      ],
      total_commits: 2,
      semantic_summary: {
        files: { added: [], removed: ["old.txt"], changed: ["model.pt"] },
        totals: { bytes_before: 105, bytes_after: 100 },
        summary: "+0 -1 ~1 file(s)",
      },
      env_snapshot_id: null,
      avh_object_id: null,
      ...overrides,
    };
  }

  function mockDetailApi(summaryOverrides: Partial<api.RunSummary> = {}) {
    mocked.fetchRuns.mockResolvedValue([childRun]);
    mocked.fetchLatestEventId.mockResolvedValue(0);
    mocked.fetchRunSummary.mockResolvedValue(baseSummary(summaryOverrides));
  }

  async function open() {
    render(<RunsPanel projectId="p" />);
    await waitFor(() => expect(screen.getByText("fine-tune")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId(`run-row-${childRun.id.slice(0, 8)}`));
    await waitFor(() => expect(screen.getByText(/Linked commits/)).toBeInTheDocument());
  }

  it("shows the parent lineage chain", async () => {
    mockDetailApi();
    await open();
    expect(screen.getByText(/↳ child000 \(fine-tune\)/)).toBeInTheDocument();
    expect(screen.getByText(/↳ +parent00 \(baseline\)/)).toBeInTheDocument();
  });

  it("lists linked commits with messages and a metric table (below the chart threshold)", async () => {
    mockDetailApi();
    await open();
    expect(screen.getByText("latest work")).toBeInTheDocument();
    expect(screen.getByText("earlier work")).toBeInTheDocument();
    expect(screen.getByText("loss")).toBeInTheDocument();
    expect(screen.getAllByText("0.2").length).toBeGreaterThan(0);
    expect(screen.getAllByText("0.5").length).toBeGreaterThan(0);
  });

  it("switches to a metrics chart once there are 3+ points of history", async () => {
    mockDetailApi({
      commits: [
        { hash: "c" + "0".repeat(63), message: "c0", metrics: { loss: 0.2 }, timestamp: "2026-08-25T03:00:00" },
        { hash: "c" + "1".repeat(63), message: "c1", metrics: { loss: 0.3 }, timestamp: "2026-08-25T02:00:00" },
        { hash: "c" + "2".repeat(63), message: "c2", metrics: { loss: 0.5 }, timestamp: "2026-08-25T01:00:00" },
      ],
      total_commits: 3,
    });
    await open();
    // MetricsChart renders "ML Metrics Over Time" as its card title; the plain
    // per-commit table (with a "Message" column header) should NOT be present.
    expect(screen.getByText("ML Metrics Over Time")).toBeInTheDocument();
    expect(screen.queryByText("Message")).not.toBeInTheDocument();
  });

  it("shows the server-computed semantic summary", async () => {
    mockDetailApi();
    await open();
    const text = screen.getByText(/Latest change/i).parentElement!.textContent ?? "";
    expect(text).toContain("+0 -1 ~1 file(s)");
    expect(text).toContain("105");
    expect(text).toContain("100");
  });

  it("shows a needs-more-history message with fewer than two commits", async () => {
    mockDetailApi({
      commits: [{ hash: "c" + "0".repeat(63), message: "only one", metrics: {}, timestamp: null }],
      total_commits: 1,
      semantic_summary: null,
    });
    await open();
    expect(screen.getByText(/needs at least two linked commits/i)).toBeInTheDocument();
  });

  it("shows env snapshot and published-notes pointers when present", async () => {
    mockDetailApi({
      env_snapshot_id: "e".repeat(64),
      avh_object_id: "a".repeat(64),
    });
    await open();
    expect(screen.getByText(/env snapshot:/)).toBeInTheDocument();
    expect(screen.getByText(/context notes published/i)).toBeInTheDocument();
  });

  it("shows an opt-in hint when no context notes have been published", async () => {
    mockDetailApi(); // avh_object_id: null by default
    await open();
    expect(screen.getByText(/av handoff --publish/)).toBeInTheDocument();
  });

  it("surfaces summary fetch failures with a retry button, not blank space", async () => {
    mocked.fetchRuns.mockResolvedValue([childRun]);
    mocked.fetchLatestEventId.mockResolvedValue(0);
    mocked.fetchRunSummary.mockRejectedValue(new Error("HTTP 404"));
    render(<RunsPanel projectId="p" />);
    await waitFor(() => expect(screen.getByText("fine-tune")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId(`run-row-${childRun.id.slice(0, 8)}`));
    await waitFor(() => expect(screen.getByText(/HTTP 404/)).toBeInTheDocument());
    expect(screen.getByText("Retry")).toBeInTheDocument();
  });

  it("updates the URL's ?run= param on open and clears it on close", async () => {
    mockDetailApi();
    await open();
    expect(new URLSearchParams(window.location.search).get("run")).toBe(childRun.id);

    fireEvent.click(screen.getByText("Close"));
    await waitFor(() => expect(screen.queryByText(/Linked commits/)).not.toBeInTheDocument());
    expect(new URLSearchParams(window.location.search).get("run")).toBeNull();
  });

  it("opens the detail panel immediately when given an initialRunId (deep link)", async () => {
    mockDetailApi();
    render(<RunsPanel projectId="p" initialRunId={childRun.id} />);
    await waitFor(() => expect(screen.getByText(/Linked commits/)).toBeInTheDocument());
    expect(mocked.fetchRunSummary).toHaveBeenCalledWith(childRun.id);
  });

  it("shows a policy-outcome badge when the run has one", async () => {
    mockDetailApi({
      run: { ...childRun, policy_outcome: { decision: "deny", rule: "metric:loss<", at: "2026-08-25T02:00:00" } },
    });
    await open();
    expect(screen.getByText("policy: deny")).toBeInTheDocument();
  });

  it("shows no policy-outcome badge when the run has never had a decision recorded", async () => {
    mockDetailApi(); // childRun has no policy_outcome
    await open();
    expect(screen.queryByText(/^policy: /)).not.toBeInTheDocument();
  });

  it('offers "Compare weights" once at least two commits are linked, ordering older before newer', async () => {
    const onCompareWeights = vi.fn();
    mockDetailApi(); // two commits: newest-first in the payload (c0 @ 02:00, c1 @ 01:00)
    render(<RunsPanel projectId="p" onCompareWeights={onCompareWeights} />);
    await waitFor(() => expect(screen.getByText("fine-tune")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId(`run-row-${childRun.id.slice(0, 8)}`));
    await waitFor(() => expect(screen.getByText(/Linked commits/)).toBeInTheDocument());

    const button = screen.getByRole("button", { name: /Compare weights/ });
    fireEvent.click(button);
    expect(onCompareWeights).toHaveBeenCalledWith("c" + "1".repeat(63), "c" + "0".repeat(63));
  });

  it('omits "Compare weights" with fewer than two linked commits', async () => {
    mockDetailApi({
      commits: [{ hash: "c" + "0".repeat(63), message: "only one", metrics: {}, timestamp: null }],
      total_commits: 1,
      semantic_summary: null,
    });
    await open();
    expect(screen.queryByRole("button", { name: /Compare weights/ })).not.toBeInTheDocument();
  });

  it("omits \"Compare weights\" entirely when the caller doesn't pass onCompareWeights", async () => {
    mockDetailApi();
    await open(); // uses the default render(<RunsPanel projectId="p" />) with no callback
    expect(screen.queryByRole("button", { name: /Compare weights/ })).not.toBeInTheDocument();
  });

  it("fetches the full metrics series when there's more history than the capped summary shows", async () => {
    mockDetailApi({ total_commits: 5 }); // only 2 commits in the payload, 5 total
    mocked.fetchRunMetrics.mockResolvedValue([
      { hash: "c" + "0".repeat(63), message: "c0", metrics: { loss: 0.9 }, timestamp: "2026-08-25T01:00:00", linked_at: null },
      { hash: "c" + "1".repeat(63), message: "c1", metrics: { loss: 0.7 }, timestamp: "2026-08-25T02:00:00", linked_at: null },
      { hash: "c" + "2".repeat(63), message: "c2", metrics: { loss: 0.5 }, timestamp: "2026-08-25T03:00:00", linked_at: null },
      { hash: "c" + "3".repeat(63), message: "c3", metrics: { loss: 0.3 }, timestamp: "2026-08-25T04:00:00", linked_at: null },
      { hash: "c" + "4".repeat(63), message: "c4", metrics: { loss: 0.1 }, timestamp: "2026-08-25T05:00:00", linked_at: null },
    ]);
    await open();
    await waitFor(() => expect(mocked.fetchRunMetrics).toHaveBeenCalledWith(childRun.id));
    // 5 real points is enough to cross the chart threshold and replace the 2-row table.
    await waitFor(() => expect(screen.getByText("ML Metrics Over Time")).toBeInTheDocument());
    expect(screen.getByText(/full history/)).toBeInTheDocument();
  });

  it("never calls fetchRunMetrics when the capped summary already has everything", async () => {
    mockDetailApi(); // total_commits: 2, commits.length: 2 — nothing more to fetch
    await open();
    expect(mocked.fetchRunMetrics).not.toHaveBeenCalled();
  });
});
