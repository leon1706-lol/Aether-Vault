import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { RunsPanel } from "../RunsPanel";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchRuns: vi.fn(),
  fetchLatestEventId: vi.fn(),
  fetchRun: vi.fn(),
  fetchCommit: vi.fn(),
}));

const mocked = vi.mocked(api);

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
// v1.2.2 Run detail (expandable row → lineage + linked commits + client-side summary)
// ---------------------------------------------------------------------------

describe("RunsPanel detail", () => {
  const childRun = baseRun({
    id: "child000000000000000000000000001",
    name: "fine-tune",
    parent_run_id: "parent0000000000000000000000001",
    commit_hashes: [
      "c" + "0".repeat(63),
      "c" + "1".repeat(63),
    ],
    metrics_summary: { loss: 0.2 },
  });
  const parentRun = baseRun({
    id: "parent0000000000000000000000001",
    name: "baseline",
    parent_run_id: null,
  });

  function mockDetailApi() {
    mocked.fetchRuns.mockResolvedValue([childRun]);
    mocked.fetchLatestEventId.mockResolvedValue(0);
    mocked.fetchRun.mockImplementation(async (id) =>
      id === childRun.id ? childRun : parentRun
    );
    mocked.fetchCommit.mockImplementation(async (hash) => ({
      hash,
      message: hash.endsWith("0") ? "latest work" : "earlier work",
      author: "agent",
      timestamp: hash.endsWith("0") ? "2026-08-25T02:00:00" : "2026-08-25T01:00:00",
      parent_hash: null,
      root_tree_hash: null,
      tags: [],
      metrics: { loss: hash.endsWith("0") ? 0.2 : 0.5 },
      tree:
        hash.endsWith("0")
          ? { "model.pt": { hash: "n1", size: 100, type: "artifact", chunks: [{ hash: "ch1" }, { hash: "ch2" }] } }
          : { "model.pt": { hash: "n0", size: 100, type: "artifact", chunks: [{ hash: "ch1" }, { hash: "ch3" }] }, "old.txt": { hash: "o", size: 5, type: "code", chunks: [] } },
    }));
  }

  async function expand() {
    render(<RunsPanel projectId="p" />);
    await waitFor(() => expect(screen.getByText("fine-tune")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId(`run-row-${childRun.id.slice(0, 8)}`));
    await waitFor(() =>
      expect(screen.getByText(/Linked commits/)).toBeInTheDocument()
    );
  }

  it("shows the parent lineage chain", async () => {
    mockDetailApi();
    await expand();
    // Chain rendered self → root: child row, then the parent indented with its name.
    // ids slice(0,8): "child000" / "parent00".
    expect(screen.getByText(/↳ child000 \(fine-tune\)/)).toBeInTheDocument();
    expect(screen.getByText(/↳ +parent00 \(baseline\)/)).toBeInTheDocument();
  });

  it("lists linked commits with messages and a metric table", async () => {
    mockDetailApi();
    await expand();
    expect(screen.getByText("latest work")).toBeInTheDocument();
    expect(screen.getByText("earlier work")).toBeInTheDocument();
    // union of metric keys becomes columns; both rows' values render
    expect(screen.getByText("loss")).toBeInTheDocument();
    expect(screen.getAllByText("0.2").length).toBeGreaterThan(0);
    expect(screen.getAllByText("0.5").length).toBeGreaterThan(0);
  });

  it("composes the semantic summary from the last two linked commits' trees", async () => {
    mockDetailApi();
    await expand();
    // old tree {model.pt(ch1,ch3), old.txt} → new tree {model.pt(ch1,ch2)}:
    // 0 added · 1 removed (old.txt) · 1 changed file; chunks reused 1/2 → dedup 50%.
    const text = screen.getByText(/Latest change/i).parentElement!.textContent ?? "";
    expect(text).toContain("0 added");
    expect(text).toContain("1 changed");
    expect(text).toContain("1 removed");
    expect(text).toContain("chunks reused 1/2");
    expect(text).toMatch(/dedup 50\.0%/);
  });

  it("surfaces per-run fetch failures instead of blank space", async () => {
    mocked.fetchRuns.mockResolvedValue([childRun]);
    mocked.fetchLatestEventId.mockResolvedValue(0);
    mocked.fetchRun.mockRejectedValue(new Error("HTTP 404"));
    render(<RunsPanel projectId="p" />);
    await waitFor(() => expect(screen.getByText("fine-tune")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId(`run-row-${childRun.id.slice(0, 8)}`));
    await waitFor(() => expect(screen.getByText(/HTTP 404/)).toBeInTheDocument());
  });
});
