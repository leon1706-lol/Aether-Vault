"use client";

import { useEffect, useState } from "react";
import {
  fetchChangeSets,
  fetchImproverVersions,
  type ChangeSet,
  type ImproverVersion,
} from "@/lib/api";

interface Props {
  projectId: string | null;
}

const RISK_COLORS: Record<string, string> = {
  low: "#68d391",
  medium: "#f6ad55",
  high: "#fc8181",
};

const STATUS_COLORS: Record<ChangeSet["status"], string> = {
  proposed: "var(--text-muted)",
  approved: "#4fd1c5",
  applied: "#68d391",
  rejected: "#fc8181",
  rolled_back: "#f6ad55",
};

// v1.3.1 (RSI R6, WP-38): improver lineage + pending self-edits, the WebUI counterpart
// of `av improver list/show/lineage` and `av improver propose/review` — see
// development/architecture.md's "Improver Artifact Contract".
export function ImproverPanel({ projectId }: Props) {
  const [improvers, setImprovers] = useState<ImproverVersion[]>([]);
  const [changeSets, setChangeSets] = useState<ChangeSet[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([
      fetchImproverVersions({ projectId, limit: 50 }),
      fetchChangeSets({ projectId, limit: 100 }),
    ])
      .then(([improverRows, changeSetRows]) => {
        if (cancelled) return;
        setImprovers(improverRows);
        setChangeSets(changeSetRows);
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

  const byId = new Map(improvers.map((i) => [i.id, i]));
  const pending = changeSets.filter(
    (cs) => cs.status === "proposed" || cs.status === "approved"
  );

  return (
    <div className="grid-2">
      <div className="card">
        <div className="section-header">
          <span className="card-title">
            <ImproverIcon />
            Improver Lineage
          </span>
          <span className="section-count">{improvers.length}</span>
        </div>
        {loading ? (
          <div className="loading-overlay">
            <div className="spinner" />
            Loading improver versions…
          </div>
        ) : error ? (
          <div className="empty-state">Failed to load: {error}</div>
        ) : improvers.length === 0 ? (
          <div className="empty-state">
            <ImproverIcon size={32} />
            <span>No improver versions registered yet</span>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Run <code style={{ fontSize: 11 }}>av improver init</code> to register the
              first one.
            </span>
          </div>
        ) : (
          <div className="checkpoint-list">
            {improvers.map((v) => {
              const parent = v.parent_id ? byId.get(v.parent_id) : null;
              return (
                <div key={v.id} className="checkpoint-row" style={{ padding: "10px 8px" }}>
                  <span className="checkpoint-iter" title={v.id}>
                    {v.id.slice(0, 8)}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, color: "var(--text-primary)" }}>
                      {v.parent_id ? `parent ${v.parent_id.slice(0, 8)}${parent ? "" : " (older)"}` : "root version"}
                    </div>
                    <div style={{ fontSize: 11.5, color: "var(--text-secondary)" }}>
                      {v.created_by || "unknown"} · {v.created_at ? new Date(v.created_at).toLocaleString() : "—"}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="card">
        <div className="section-header">
          <span className="card-title">
            <ChangeSetIcon />
            Pending Self-Edits
          </span>
          <span className="section-count">{pending.length}</span>
        </div>
        {loading ? (
          <div className="loading-overlay">
            <div className="spinner" />
            Loading change sets…
          </div>
        ) : error ? (
          <div className="empty-state">Failed to load: {error}</div>
        ) : pending.length === 0 ? (
          <div className="empty-state">
            <ChangeSetIcon size={32} />
            <span>No self-edits awaiting review or application</span>
          </div>
        ) : (
          <div className="checkpoint-list">
            {pending.map((cs) => (
              <div key={cs.id} className="checkpoint-row" style={{ padding: "10px 8px" }}>
                <span
                  className="checkpoint-iter"
                  title={cs.risk ?? "unknown risk"}
                  style={{ color: cs.risk ? RISK_COLORS[cs.risk] : undefined }}
                >
                  {(cs.risk ?? "?").toUpperCase()}
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, color: "var(--text-primary)" }}>
                    {cs.id.slice(0, 8)}
                    {cs.improver_id ? ` → improver ${cs.improver_id.slice(0, 8)}` : ""}
                  </div>
                  <div style={{ fontSize: 11.5, color: "var(--text-secondary)" }}>
                    {cs.created_by || "unknown"} ·{" "}
                    {cs.created_at ? new Date(cs.created_at).toLocaleString() : "—"}
                  </div>
                </div>
                <span style={{ fontSize: 11.5, fontWeight: 600, color: STATUS_COLORS[cs.status] }}>
                  {cs.status}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ImproverIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="6" cy="6" r="3" />
      <circle cx="6" cy="18" r="3" />
      <path d="M6 9v6" />
      <circle cx="18" cy="12" r="3" />
      <path d="M9 6h4a2 2 0 012 2v1" />
      <path d="M9 18h4a2 2 0 002-2v-1" />
    </svg>
  );
}

function ChangeSetIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.12 2.12 0 013 3L7 19l-4 1 1-4z" />
    </svg>
  );
}
