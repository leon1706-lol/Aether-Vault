"use client";

import { useMemo, useState, type CSSProperties } from "react";
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
import { extractMetricKeys } from "@/components/MetricsChart";
import { shortHash, type Commit, type Ref } from "@/lib/api";
import { indexByHash, reachableFromTip } from "@/lib/branchGraph";

interface Props {
  commits: Commit[];
  refs: Ref;
  loading: boolean;
}

const METRIC_COLORS = ["#ff7a1a", "#ffb380", "#4fd1c5", "#ffd166", "#68d391", "#fc8181"];

export function MetricsPanel({ commits, refs, loading }: Props) {
  const allMetricKeys = useMemo(() => extractMetricKeys(commits), [commits]);
  const [visibleKeys, setVisibleKeys] = useState<Set<string> | null>(null);
  const [branchFilter, setBranchFilter] = useState<string>("__all__");

  const commitByHash = useMemo(() => indexByHash(commits), [commits]);

  const { scopedCommits, truncated } = useMemo(() => {
    if (branchFilter === "__all__") return { scopedCommits: commits, truncated: false };
    const tip = refs[branchFilter];
    const { hashes, truncated: t } = reachableFromTip(tip, commitByHash);
    return { scopedCommits: commits.filter((c) => hashes.has(c.hash)), truncated: t };
  }, [branchFilter, commits, refs, commitByHash]);

  const metricKeys = useMemo(
    () => (visibleKeys ? allMetricKeys.filter((k) => visibleKeys.has(k)) : allMetricKeys),
    [allMetricKeys, visibleKeys]
  );

  const sorted = useMemo(
    () =>
      [...scopedCommits].sort((a, b) => {
        const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
        const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
        return ta - tb;
      }),
    [scopedCommits]
  );

  const tableRows = sorted.filter((c) => Object.keys(c.metrics ?? {}).length > 0);

  const chartData = tableRows.map((c) => ({
    label: shortHash(c.hash),
    ...Object.fromEntries(
      metricKeys.map((k) => [k, typeof c.metrics?.[k] === "number" ? c.metrics[k] : null])
    ),
  }));

  function toggleKey(key: string) {
    setVisibleKeys((prev) => {
      const base = prev ?? new Set(allMetricKeys);
      const next = new Set(base);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  if (loading && commits.length === 0) {
    return (
      <div className="card">
        <div className="loading-overlay">
          <div className="spinner" />
          Loading metrics…
        </div>
      </div>
    );
  }

  if (allMetricKeys.length === 0) {
    return (
      <div className="card">
        <div className="empty-state">
          <span>No metrics yet</span>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            Attach metrics using{" "}
            <code style={{ fontSize: 11 }}>--metric sharpe=2.45</code>
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="section fade-in fade-in-1">
      <div className="card">
        <div className="section-header">
          <span className="card-title">ML Metrics Over Time</span>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <label htmlFor="metrics-branch-select" className="diff-toolbar-label">
              Branch
            </label>
            <select
              id="metrics-branch-select"
              value={branchFilter}
              onChange={(e) => setBranchFilter(e.target.value)}
            >
              <option value="__all__">All branches</option>
              {Object.keys(refs).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
        </div>

        {branchFilter !== "__all__" && truncated && (
          <div className="diff-warning">
            This branch&apos;s history extends beyond the currently loaded commit window —
            counts/series above reflect only loaded history.
          </div>
        )}

        <div className="commit-badges" style={{ marginBottom: 14 }}>
          {allMetricKeys.map((key, i) => {
            const active = metricKeys.includes(key);
            return (
              <button
                key={key}
                type="button"
                className="tag-pill metric"
                style={{
                  cursor: "pointer",
                  border: "none",
                  opacity: active ? 1 : 0.4,
                  background: active
                    ? "rgba(255,255,255,0.07)"
                    : "rgba(255,255,255,0.02)",
                }}
                onClick={() => toggleKey(key)}
              >
                <span
                  style={{
                    display: "inline-block",
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    background: METRIC_COLORS[i % METRIC_COLORS.length],
                    marginRight: 5,
                  }}
                />
                {key}
              </button>
            );
          })}
        </div>

        <div className="chart-container" style={{ height: 420 }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 4, right: 12, left: -8, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
              <XAxis
                dataKey="label"
                tick={{ fill: "#718096", fontSize: 11, fontFamily: "'JetBrains Mono', monospace" }}
                axisLine={{ stroke: "rgba(255,255,255,0.08)" }}
                tickLine={false}
              />
              <YAxis tick={{ fill: "#718096", fontSize: 11 }} axisLine={false} tickLine={false} width={48} />
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
              <Legend wrapperStyle={{ fontSize: 12, color: "#718096", paddingTop: 8 }} />
              {metricKeys.map((key) => {
                const colorIdx = allMetricKeys.indexOf(key);
                const color = METRIC_COLORS[colorIdx % METRIC_COLORS.length];
                return (
                  <Line
                    key={key}
                    type="monotone"
                    dataKey={key}
                    stroke={color}
                    strokeWidth={2}
                    dot={{ fill: color, r: 4, strokeWidth: 0 }}
                    activeDot={{ r: 6, strokeWidth: 0 }}
                    connectNulls
                  />
                );
              })}
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="card section" style={{ marginTop: 20 }}>
        <div className="section-header">
          <span className="card-title">Metrics Table</span>
          <span className="section-count">{tableRows.length} commits</span>
        </div>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
            <thead>
              <tr>
                <th style={tableHeadStyle}>Hash</th>
                <th style={tableHeadStyle}>Author</th>
                <th style={tableHeadStyle}>Time</th>
                {allMetricKeys.map((k) => (
                  <th key={k} style={tableHeadStyle}>{k}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {tableRows.map((c) => (
                <tr key={c.hash}>
                  <td style={tableCellStyle} className="mono">{shortHash(c.hash)}</td>
                  <td style={tableCellStyle}>{c.author}</td>
                  <td style={tableCellStyle}>
                    {c.timestamp ? new Date(c.timestamp).toLocaleString() : "—"}
                  </td>
                  {allMetricKeys.map((k) => (
                    <td key={k} style={tableCellStyle}>
                      {typeof c.metrics?.[k] === "number"
                        ? (c.metrics[k] as number).toFixed(3)
                        : c.metrics?.[k] ?? "—"}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

const tableHeadStyle: CSSProperties = {
  textAlign: "left",
  padding: "8px 10px",
  borderBottom: "1px solid var(--border)",
  color: "var(--text-secondary)",
  fontWeight: 600,
  whiteSpace: "nowrap",
};

const tableCellStyle: CSSProperties = {
  padding: "8px 10px",
  borderBottom: "1px solid var(--border)",
  color: "var(--text-primary)",
  whiteSpace: "nowrap",
};
