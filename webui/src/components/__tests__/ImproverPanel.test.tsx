// v1.3.1 (RSI R6, WP-38): ImproverPanel — lineage + pending self-edits.
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

import { ImproverPanel } from "../ImproverPanel";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchImproverVersions: vi.fn(),
  fetchChangeSets: vi.fn(),
}));

const mocked = vi.mocked(api);

function improver(overrides: Partial<api.ImproverVersion> = {}): api.ImproverVersion {
  return {
    id: "aaaaaaaa-1111-1111-1111-111111111111",
    project_id: "p1",
    manifest_object_id: "f".repeat(64),
    parent_id: null,
    created_by: "alice",
    created_at: "2026-01-01T00:00:00",
    ...overrides,
  };
}

function changeSet(overrides: Partial<api.ChangeSet> = {}): api.ChangeSet {
  return {
    id: "bbbbbbbb-2222-2222-2222-222222222222",
    project_id: "p1",
    improver_id: "aaaaaaaa-1111-1111-1111-111111111111",
    object_id: "e".repeat(64),
    status: "proposed",
    risk: "low",
    created_by: "alice",
    created_at: "2026-01-01T00:00:00",
    updated_at: "2026-01-01T00:00:00",
    ...overrides,
  };
}

describe("ImproverPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a loading state before both fetches resolve", () => {
    mocked.fetchImproverVersions.mockReturnValue(new Promise(() => {}));
    mocked.fetchChangeSets.mockReturnValue(new Promise(() => {}));
    render(<ImproverPanel projectId={null} />);
    expect(screen.getAllByText(/Loading/).length).toBeGreaterThan(0);
  });

  it("shows empty states when nothing has been registered or proposed", async () => {
    mocked.fetchImproverVersions.mockResolvedValue([]);
    mocked.fetchChangeSets.mockResolvedValue([]);
    render(<ImproverPanel projectId={null} />);
    await waitFor(() =>
      expect(screen.getByText(/No improver versions registered yet/)).toBeInTheDocument()
    );
    expect(screen.getByText(/No self-edits awaiting review or application/)).toBeInTheDocument();
  });

  it("surfaces a fetch error instead of pretending everything is fine", async () => {
    mocked.fetchImproverVersions.mockRejectedValue(new Error("HTTP 500"));
    mocked.fetchChangeSets.mockResolvedValue([]);
    render(<ImproverPanel projectId={null} />);
    // Both cards share one error state (a single Promise.all), so the message renders
    // in both — the Improver Lineage card AND the Pending Self-Edits card.
    await waitFor(() =>
      expect(screen.getAllByText(/Failed to load: HTTP 500/).length).toBe(2)
    );
  });

  it("lists improver versions with their parent pointer", async () => {
    mocked.fetchImproverVersions.mockResolvedValue([
      improver({ id: "root0000-0000-0000-0000-000000000000", parent_id: null }),
      improver({
        id: "child000-0000-0000-0000-000000000000",
        parent_id: "root0000-0000-0000-0000-000000000000",
      }),
    ]);
    mocked.fetchChangeSets.mockResolvedValue([]);
    render(<ImproverPanel projectId={null} />);
    await waitFor(() => expect(screen.getByText("root version")).toBeInTheDocument());
    expect(screen.getByText(/parent root0000/)).toBeInTheDocument();
  });

  it("only counts proposed/approved change sets as pending, not applied/rejected/rolled_back", async () => {
    mocked.fetchImproverVersions.mockResolvedValue([]);
    mocked.fetchChangeSets.mockResolvedValue([
      changeSet({ id: "cs-proposed-000000000000000000000", status: "proposed" }),
      changeSet({ id: "cs-approved-000000000000000000000", status: "approved" }),
      changeSet({ id: "cs-applied-0000000000000000000000", status: "applied" }),
      changeSet({ id: "cs-rejected-000000000000000000000", status: "rejected" }),
    ]);
    render(<ImproverPanel projectId={null} />);
    await waitFor(() => expect(screen.getByText(/cs-propo/)).toBeInTheDocument());
    expect(screen.getByText(/cs-appro/)).toBeInTheDocument();
    expect(screen.queryByText(/cs-appli/)).not.toBeInTheDocument();
    expect(screen.queryByText(/cs-rejec/)).not.toBeInTheDocument();
  });

  it("re-fetches when the selected project changes", async () => {
    mocked.fetchImproverVersions.mockResolvedValue([]);
    mocked.fetchChangeSets.mockResolvedValue([]);
    const { rerender } = render(<ImproverPanel projectId="p1" />);
    await waitFor(() => expect(mocked.fetchImproverVersions).toHaveBeenCalledWith({ projectId: "p1", limit: 50 }));
    rerender(<ImproverPanel projectId="p2" />);
    await waitFor(() => expect(mocked.fetchImproverVersions).toHaveBeenCalledWith({ projectId: "p2", limit: 50 }));
  });
});
