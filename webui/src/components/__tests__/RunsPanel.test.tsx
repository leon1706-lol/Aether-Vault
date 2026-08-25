import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

import { RunsPanel } from "../RunsPanel";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchRuns: vi.fn(),
  fetchLatestEventId: vi.fn(),
}));

const mocked = vi.mocked(api);

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
      {
        id: "bbbb1111222233334444555566667777",
        project_id: "p",
        name: null,
        status: "running",
        parent_run_id: null,
        created_by: "agent-7",
        metrics_summary: {},
        created_at: "2026-08-24T11:00:00",
        completed_at: null,
      },
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
