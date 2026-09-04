// v1.3.1 (RSI R6, WP-38): CanaryPanel — status + trend.
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

import { CanaryPanel } from "../CanaryPanel";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchCanaryResults: vi.fn(),
}));

const mocked = vi.mocked(api);

function result(overrides: Partial<api.CanaryResult> = {}): api.CanaryResult {
  return {
    id: 1,
    project_id: "p1",
    improver_id: "aaaaaaaa-1111-1111-1111-111111111111",
    suite_object_id: "f".repeat(64),
    passed: true,
    details: {},
    run_id: null,
    created_at: "2026-01-01T00:00:00",
    ...overrides,
  };
}

describe("CanaryPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a loading state before the fetch resolves", () => {
    mocked.fetchCanaryResults.mockReturnValue(new Promise(() => {}));
    render(<CanaryPanel projectId={null} />);
    expect(screen.getByText(/Loading canary results/i)).toBeInTheDocument();
  });

  it("shows an empty state when nothing has been recorded", async () => {
    mocked.fetchCanaryResults.mockResolvedValue([]);
    render(<CanaryPanel projectId={null} />);
    await waitFor(() =>
      expect(screen.getByText(/No canary results recorded yet/)).toBeInTheDocument()
    );
  });

  it("surfaces a fetch error instead of pretending everything is fine", async () => {
    mocked.fetchCanaryResults.mockRejectedValue(new Error("HTTP 500"));
    render(<CanaryPanel projectId={null} />);
    await waitFor(() => expect(screen.getByText(/Failed to load: HTTP 500/)).toBeInTheDocument());
  });

  it("shows a pass/total badge and PASS/FAIL rows", async () => {
    mocked.fetchCanaryResults.mockResolvedValue([
      result({ id: 1, passed: true }),
      result({ id: 2, passed: false }),
      result({ id: 3, passed: true }),
    ]);
    render(<CanaryPanel projectId={null} />);
    await waitFor(() => expect(screen.getByText("2/3")).toBeInTheDocument());
    expect(screen.getAllByText("PASS").length).toBe(2);
    expect(screen.getAllByText("FAIL").length).toBe(1);
  });

  it("passes the improver-scoped project id through to the fetch", async () => {
    mocked.fetchCanaryResults.mockResolvedValue([]);
    render(<CanaryPanel projectId="p1" limit={5} />);
    await waitFor(() =>
      expect(mocked.fetchCanaryResults).toHaveBeenCalledWith({ projectId: "p1", limit: 5 })
    );
  });
});
