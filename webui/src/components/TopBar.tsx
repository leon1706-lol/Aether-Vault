"use client";

import type { HealthResponse } from "@/lib/api";

interface Props {
  health: HealthResponse | null;
  loading: boolean;
  onRefresh: () => void;
}

export function TopBar({ health, loading, onRefresh }: Props) {
  const isOnline = health?.status === "ok";
  const apiUrl =
    process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

  return (
    <div className="top-bar">
      <span className="top-bar-title">Dashboard</span>

      <div className="top-bar-right">
        <span className="server-url" title="FastAPI server URL">
          {apiUrl}
        </span>

        <div className={`status-badge ${isOnline ? "online" : "offline"}`}>
          <div className="status-dot" />
          {loading ? "Connecting…" : isOnline ? `Server Online · v${health?.version ?? "?"}` : "Server Offline"}
        </div>

        <button
          id="btn-refresh"
          className="btn btn-ghost"
          onClick={onRefresh}
          title="Refresh data"
          style={{ padding: "6px 12px" }}
        >
          <RefreshIcon loading={loading} />
          Refresh
        </button>
      </div>
    </div>
  );
}

function RefreshIcon({ loading }: { loading: boolean }) {
  return (
    <svg
      width="14" height="14"
      viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0, animation: loading ? "spin 0.8s linear infinite" : "none" }}
    >
      <polyline points="23 4 23 10 17 10" />
      <polyline points="1 20 1 14 7 14" />
      <path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" />
    </svg>
  );
}
