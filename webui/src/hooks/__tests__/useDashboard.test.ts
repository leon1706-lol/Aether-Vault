// v1.2.5: useDashboard had no test coverage at all — closing that gap alongside
// ProjectsPanel (the other identified untested surface). Lives under src/hooks/__tests__
// so vitest.config.ts's environmentMatchGlobs picks up jsdom (renderHook needs a DOM).
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { useDashboard } from "../useDashboard";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchDashboardData: vi.fn(),
}));

const mocked = vi.mocked(api);

function dashboardData(overrides: Partial<api.DashboardData> = {}): api.DashboardData {
  return {
    health: { status: "ok", version: "1.2.5" },
    refs: { main: "a".repeat(64) },
    commits: [],
    stats: null,
    error: null,
    ...overrides,
  };
}

describe("useDashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts in a loading state with no data", async () => {
    mocked.fetchDashboardData.mockReturnValue(new Promise(() => {})); // never resolves
    const { result } = renderHook(() => useDashboard(60_000, null));
    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();
  });

  it("loads data on mount and flips loading off", async () => {
    mocked.fetchDashboardData.mockResolvedValue(dashboardData());
    const { result } = renderHook(() => useDashboard(60_000, null));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data?.health?.status).toBe("ok");
    expect(mocked.fetchDashboardData).toHaveBeenCalledWith(null);
  });

  it("passes the projectId through to fetchDashboardData", async () => {
    mocked.fetchDashboardData.mockResolvedValue(dashboardData());
    renderHook(() => useDashboard(60_000, "proj-42"));
    await waitFor(() => expect(mocked.fetchDashboardData).toHaveBeenCalledWith("proj-42"));
  });

  it("re-fetches on the given interval", async () => {
    mocked.fetchDashboardData.mockResolvedValue(dashboardData());
    renderHook(() => useDashboard(1_000, null));
    await waitFor(() => expect(mocked.fetchDashboardData).toHaveBeenCalledTimes(1));

    await act(async () => {
      vi.advanceTimersByTime(1_000);
    });
    await waitFor(() => expect(mocked.fetchDashboardData).toHaveBeenCalledTimes(2));
  });

  it("re-fetches immediately when projectId changes (new effect run)", async () => {
    mocked.fetchDashboardData.mockResolvedValue(dashboardData());
    const { rerender } = renderHook(
      ({ projectId }: { projectId: string | null }) => useDashboard(60_000, projectId),
      { initialProps: { projectId: null as string | null } },
    );
    await waitFor(() => expect(mocked.fetchDashboardData).toHaveBeenCalledWith(null));

    rerender({ projectId: "proj-2" });
    await waitFor(() => expect(mocked.fetchDashboardData).toHaveBeenCalledWith("proj-2"));
  });

  it("refresh() re-runs the fetch on demand without waiting for the interval", async () => {
    mocked.fetchDashboardData.mockResolvedValue(dashboardData());
    const { result } = renderHook(() => useDashboard(60_000, null));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocked.fetchDashboardData).toHaveBeenCalledTimes(1);

    await act(async () => {
      await result.current.refresh();
    });
    expect(mocked.fetchDashboardData).toHaveBeenCalledTimes(2);
  });

  it("stops polling after unmount", async () => {
    mocked.fetchDashboardData.mockResolvedValue(dashboardData());
    const { unmount } = renderHook(() => useDashboard(1_000, null));
    await waitFor(() => expect(mocked.fetchDashboardData).toHaveBeenCalledTimes(1));

    unmount();
    await act(async () => {
      vi.advanceTimersByTime(5_000);
    });
    expect(mocked.fetchDashboardData).toHaveBeenCalledTimes(1); // no further calls
  });
});
