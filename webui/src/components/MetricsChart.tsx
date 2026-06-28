"use client";

import { useMemo } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import { type Commit, shortHash } from "@/lib/api";

interface Props {
  commits: Commit[];
  loading: boolean;
}

const METRIC_COLORS: Record<number, string> = {
  0: "#ff7a1a",
  1: "#ffb380",
  2: "#4fd1c5",
  3: "#ffd166",
  4: "#68d391",
  5: "#fc8181",
};

export function extractMetricKeys(commits: Commit[]): string[] {
  return [
    ...new Set(
      commits.flatMap((c) =>
        Object.entries(c.metrics ?? {})
          .filter(([, v]) => typeof v === "number")
          .map(([k]) => k)
      )
    ),
  ].slice(0, 6);
}

export function MetricsChart({ commits, loading }: Props) {
  const metricKeys = useMemo(() => extractMetricKeys(commits), [commits]);

  if (loading && commits.length === 0) {
    return (
      <div className="card">
        <div className="card-header">
          <span className="card-title">
            <ChartIcon />
            ML Metrics Over Time
          </span>
        </div>
        <div className="loading-overlay">
          <div className="spinner" />
          Loading metrics…
        </div>
      </div>
    );
  }

  const hasMetrics =
    metricKeys.length > 0 &&
    commits.some((c) => Object.keys(c.metrics ?? {}).length > 0);

  if (!hasMetrics) {
    return (
      <div className="card">
        <div className="card-header">
          <span className="card-title">
            <ChartIcon />
            ML Metrics Over Time
          </span>
        </div>
        <div className="empty-state">
          <ChartIcon size={32} />
          <span>No metrics yet</span>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            Attach metrics using{" "}
            <code style={{ fontSize: 11 }}>--metric sharpe=2.45</code>
          </span>
        </div>
      </div>
    );
  }

  // Build chart data sorted oldest → newest
  const sorted = [...commits].sort((a, b) => {
    const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
    const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
    return ta - tb;
  });

  const chartData = sorted
    .filter((c) => Object.keys(c.metrics ?? {}).length > 0)
    .map((c, idx) => ({
      idx: idx + 1,
      label: shortHash(c.hash),
      ...Object.fromEntries(
        metricKeys.map((k) => [
          k,
          typeof c.metrics?.[k] === "number" ? c.metrics[k] : null,
        ])
      ),
    }));

  return (
    <div className="card">
      <div className="section-header">
        <span className="card-title">
          <ChartIcon />
          ML Metrics Over Time
        </span>
        <span className="section-count">{metricKeys.length} metrics</span>
      </div>

      <div className="chart-container" style={{ height: 250 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={chartData}
            margin={{ top: 4, right: 12, left: -8, bottom: 0 }}
          >
            <CartesianGrid
              strokeDasharray="3 3"
              stroke="rgba(255,255,255,0.05)"
            />
            <XAxis
              dataKey="label"
              tick={{ fill: "#718096", fontSize: 11, fontFamily: "'JetBrains Mono', monospace" }}
              axisLine={{ stroke: "rgba(255,255,255,0.08)" }}
              tickLine={false}
            />
            <YAxis
              tick={{ fill: "#718096", fontSize: 11 }}
              axisLine={false}
              tickLine={false}
              width={48}
            />
            <Tooltip
              contentStyle={{
                background: "#0b0f1e",
                border: "1px solid rgba(255,255,255,0.1)",
                borderRadius: 8,
                fontSize: 12,
                color: "#e2e8f0",
              }}
              labelStyle={{ color: "#ff7a1a", fontFamily: "JetBrains Mono", marginBottom: 4 }}
            />
            <Legend
              wrapperStyle={{ fontSize: 12, color: "#718096", paddingTop: 8 }}
            />
            {metricKeys.map((key, i) => (
              <Line
                key={key}
                type="monotone"
                dataKey={key}
                stroke={METRIC_COLORS[i % Object.keys(METRIC_COLORS).length]}
                strokeWidth={2}
                dot={{ fill: METRIC_COLORS[i % Object.keys(METRIC_COLORS).length], r: 4, strokeWidth: 0 }}
                activeDot={{ r: 6, strokeWidth: 0 }}
                connectNulls
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function ChartIcon({ size = 16 }: { size?: number }) {
  return (
    <svg
      width={size} height={size}
      viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
    >
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
    </svg>
  );
}
