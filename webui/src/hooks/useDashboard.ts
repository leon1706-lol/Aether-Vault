"use client";

import { useState, useEffect, useCallback } from "react";
import {
  fetchDashboardData,
  type DashboardData,
} from "@/lib/api";

export function useDashboard(refreshIntervalMs = 15000, projectId?: string | null) {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    const d = await fetchDashboardData(projectId);
    setData(d);
    setLoading(false);
  }, [projectId]);

  useEffect(() => {
    setLoading(true);
    refresh();
    const id = setInterval(refresh, refreshIntervalMs);
    return () => clearInterval(id);
  }, [refresh, refreshIntervalMs]);

  return { data, loading, refresh };
}
