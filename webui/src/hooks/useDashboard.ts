"use client";

import { useState, useCallback } from "react";
import {
  fetchDashboardData,
  type DashboardData,
} from "@/lib/api";
import { usePolling, type PollingOptions } from "./usePolling";

/**
 * Dashboard data with polling that pauses while the tab is hidden and backs off
 * (15 s -> 30 -> 60 -> 120 cap by default) while the registry is failing -- see
 * usePolling for the mechanics. `paused`/`nextDelayMs` are additive return fields.
 */
export function useDashboard(
  refreshIntervalMs = 15000,
  projectId?: string | null,
  options: Pick<PollingOptions, "maxBackoffMs"> = {},
) {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    const d = await fetchDashboardData(projectId);
    setData(d);
    setLoading(false);
    // fetchDashboardData never throws (each sub-fetch falls back), so its recorded
    // `error` is the failure signal the backoff keys on.
    return d.error === null;
  }, [projectId]);

  const polling = usePolling(refresh, refreshIntervalMs, { maxBackoffMs: options.maxBackoffMs });

  return { data, loading, refresh, paused: polling.paused, nextDelayMs: polling.nextDelayMs };
}
