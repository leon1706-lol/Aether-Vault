"use client";

import { useEffect, useState } from "react";
import {
  fetchAnomalyEvents,
  fetchChangeSets,
  type AnomalyEvent,
  type ChangeSet,
} from "@/lib/api";
import { CanaryPanel } from "@/components/CanaryPanel";

interface Props {
  projectId: string | null;
}

const ANOMALY_LABELS: Record<string, string> = {
  metric_jump: "Metric jump",
  mass_rewrite: "Mass file rewrite",
  policy_change: "Policy changed",
  auth_spike: "Auth failure spike",
};

// v1.3.1 (RSI R6, WP-35/WP-36): continuous regression dashboard — canaries (embeds
// CanaryPanel), improver churn / failed self-edits (derived from `av change-sets`), and
// the anomaly event feed (`kind="anomaly"`, see development/architecture.md's "Anomaly
// Alerts Contract"). One tab, three independent server calls — no new endpoint.
export function RegressionPanel({ projectId }: Props) {
  const [changeSets, setChangeSets] = useState<ChangeSet[]>([]);
  const [anomalies, setAnomalies] = useState<AnomalyEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([
      fetchChangeSets({ projectId, limit: 200 }),
      fetchAnomalyEvents({ projectId, limit: 50 }),
    ])
      .then(([cs, ev]) => {
        if (cancelled) return;
        setChangeSets(cs);
        // Newest first for a feed — the server returns events oldest-first (ascending
        // cursor order), the opposite of every other panel's newest-first convention.
        setAnomalies([...ev].reverse());
        setError(null);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const churn = changeSets.reduce<Record<ChangeSet["status"], number>>(
    (acc, cs) => {
      acc[cs.status] = (acc[cs.status] ?? 0) + 1;
      return acc;
    },
    { proposed: 0, approved: 0, rejected: 0, applied: 0, rolled_back: 0 }
  );

  return (
    <div>
      <div className="grid-2 section fade-in fade-in-1">
        <CanaryPanel projectId={projectId} />

        <div className="card">
          <div className="section-header">
            <span className="card-title">
              <ChurnIcon />
              Improver Churn
            </span>
            <span className="section-count">{changeSets.length}</span>
          </div>
          {loading ? (
            <div className="loading-overlay">
              <div className="spinner" />
              Loading self-edit history…
            </div>
          ) : error ? (
            <div className="empty-state">Failed to load: {error}</div>
          ) : changeSets.length === 0 ? (
            <div className="empty-state">
              <ChurnIcon size={32} />
              <span>No self-edits proposed yet</span>
            </div>
          ) : (
            <div style={{ padding: "8px 8px 4px", display: "flex", flexDirection: "column", gap: 8 }}>
              {(Object.entries(churn) as [ChangeSet["status"], number][]).map(([status, count]) => (
                <div key={status} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <span style={{ width: 90, fontSize: 12, color: "var(--text-secondary)", textTransform: "capitalize" }}>
                    {status.replace("_", " ")}
                  </span>
                  <div style={{ flex: 1, height: 8, background: "var(--border)", borderRadius: 4, overflow: "hidden" }}>
                    <div
                      style={{
                        width: `${changeSets.length ? (count / changeSets.length) * 100 : 0}%`,
                        height: "100%",
                        background: status === "rejected" ? "#fc8181"
                          : status === "applied" ? "#68d391" : "#4fd1c5",
                      }}
                    />
                  </div>
                  <span style={{ width: 24, textAlign: "right", fontSize: 12, color: "var(--text-primary)" }}>
                    {count}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="section fade-in fade-in-2">
        <div className="card">
          <div className="section-header">
            <span className="card-title">
              <AnomalyIcon />
              Anomaly Feed
            </span>
            <span className="section-count">{anomalies.length}</span>
          </div>
          {loading ? (
            <div className="loading-overlay">
              <div className="spinner" />
              Loading anomaly events…
            </div>
          ) : error ? (
            <div className="empty-state">Failed to load: {error}</div>
          ) : anomalies.length === 0 ? (
            <div className="empty-state">
              <AnomalyIcon size={32} />
              <span>No anomalies detected — metric jumps, mass rewrites, policy
                changes, and auth-failure spikes will appear here.</span>
            </div>
          ) : (
            <div className="checkpoint-list">
              {anomalies.map((a) => (
                <div key={a.id} className="checkpoint-row" style={{ padding: "10px 8px" }}>
                  <span className="checkpoint-iter" style={{ color: "#f6ad55" }}>
                    !
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, color: "var(--text-primary)" }}>
                      {ANOMALY_LABELS[a.payload.type ?? ""] ?? a.payload.type ?? "Unknown"}
                    </div>
                    <div style={{ fontSize: 11.5, color: "var(--text-secondary)" }}>
                      {a.ts ? new Date(a.ts).toLocaleString() : "—"}
                      {a.project_id ? ` · project ${a.project_id.slice(0, 8)}` : ""}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ChurnIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="23 4 23 10 17 10" />
      <polyline points="1 20 1 14 7 14" />
      <path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" />
    </svg>
  );
}

function AnomalyIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}
