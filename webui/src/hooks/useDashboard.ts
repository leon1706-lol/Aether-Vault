"use client";

import { useState, useEffect, useCallback } from "react";
import {
  fetchDashboardData,
  type DashboardData,
} from "@/lib/api";

export function useDashboard(refreshIntervalMs = 15000) {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    const d = await fetchDashboardData();
    setData(d);
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, refreshIntervalMs);
    return () => clearInterval(id);
  }, [refresh, refreshIntervalMs]);

  return { data, loading, refresh };
}
