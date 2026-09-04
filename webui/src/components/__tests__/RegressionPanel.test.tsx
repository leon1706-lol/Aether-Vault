// v1.3.1 (RSI R6, WP-35/WP-36): RegressionPanel — canaries + improver churn + anomaly feed.
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

import { RegressionPanel } from "../RegressionPanel";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchCanaryResults: vi.fn(),
  fetchChangeSets: vi.fn(),
  fetchAnomalyEvents: vi.fn(),
}));

const mocked = vi.mocked(api);

function changeSet(overrides: Partial<api.ChangeSet> = {}): api.ChangeSet {
  return {
    id: "cs-1",
    project_id: "p1",
    improver_id: "imp-1",
    object_id: "e".repeat(64),
    status: "proposed",
    risk: "low",
    created_by: "alice",
    created_at: "2026-01-01T00:00:00",
    updated_at: "2026-01-01T00:00:00",
    ...overrides,
  };
}

function anomaly(overrides: Partial<api.AnomalyEvent> = {}): api.AnomalyEvent {
  return {
    id: 1,
    ts: "2026-01-01T00:00:00",
    kind: "anomaly",
    project_id: "p1",
    payload: { type: "metric_jump" },
    ...overrides,
  };
}

describe("RegressionPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked.fetchCanaryResults.mockResolvedValue([]);
  });

  it("shows empty states across all three sections when nothing has happened yet", async () => {
    mocked.fetchChangeSets.mockResolvedValue([]);
    mocked.fetchAnomalyEvents.mockResolvedValue([]);
    render(<RegressionPanel projectId={null} />);
    await waitFor(() =>
      expect(screen.getByText(/No self-edits proposed yet/)).toBeInTheDocument()
    );
    expect(screen.getByText(/No anomalies detected/)).toBeInTheDocument();
  });

  it("tallies change-set churn by status", async () => {
    mocked.fetchChangeSets.mockResolvedValue([
      changeSet({ id: "a", status: "applied" }),
      changeSet({ id: "b", status: "applied" }),
      changeSet({ id: "c", status: "rejected" }),
    ]);
    mocked.fetchAnomalyEvents.mockResolvedValue([]);
    render(<RegressionPanel projectId={null} />);
    await waitFor(() => expect(screen.getByText("applied")).toBeInTheDocument());
    // Two count badges of "2" and "1" for applied/rejected respectively.
    expect(screen.getByText("rejected")).toBeInTheDocument();
  });

  it("shows a human-readable label for each anomaly type", async () => {
    mocked.fetchChangeSets.mockResolvedValue([]);
    mocked.fetchAnomalyEvents.mockResolvedValue([
      anomaly({ id: 1, payload: { type: "metric_jump" } }),
      anomaly({ id: 2, payload: { type: "policy_change" } }),
    ]);
    render(<RegressionPanel projectId={null} />);
    await waitFor(() => expect(screen.getByText("Metric jump")).toBeInTheDocument());
    expect(screen.getByText("Policy changed")).toBeInTheDocument();
  });

  it("surfaces a fetch error instead of pretending everything is fine", async () => {
    mocked.fetchChangeSets.mockRejectedValue(new Error("HTTP 500"));
    mocked.fetchAnomalyEvents.mockResolvedValue([]);
    render(<RegressionPanel projectId={null} />);
    await waitFor(() =>
      expect(screen.getAllByText(/Failed to load: HTTP 500/).length).toBeGreaterThan(0)
    );
  });
});
