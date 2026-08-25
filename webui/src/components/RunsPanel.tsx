"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchLatestEventId, fetchRuns, type Run } from "@/lib/api";

interface Props {
  projectId: string | null;
  /** Test seam: poll intervals in ms (defaults match production cadence). */
  runsPollMs?: number;
  eventsPollMs?: number;
}

const STATUS_COLORS: Record<Run["status"], string> = {
  created: "var(--text-muted)",
  running: "#4fd1c5",
  completed: "#68d391",
  failed: "#fc8181",
};

// Runs tab (v1.2.0): first-class experiment grouping. Lists registry runs for the
// selected project (or all), with a live badge driven by the event-stream cursor —
// an agent pushing elsewhere makes this dot pulse without any manual refresh.
export function RunsPanel({ projectId, runsPollMs = 15_000, eventsPollMs = 10_000 }: Props) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newEvents, setNewEvents] = useState(false);
  const lastEventId = useRef(0);

  const load = useCallback(async () => {
    try {
      setRuns(await fetchRuns({ projectId, limit: 100 }));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId]);

  useEffect(() => {
    load();
    const id = setInterval(load, runsPollMs);
    return () => clearInterval(id);
  }, [load]);

  useEffect(() => {
    const id = setInterval(async () => {
      try {
        const latest = await fetchLatestEventId();
        if (lastEventId.current && latest > lastEventId.current) setNewEvents(true);
        lastEventId.current = Math.max(lastEventId.current, latest);
      } catch {
        /* events endpoint optional */
      }
    }, eventsPollMs);
    return () => clearInterval(id);
  }, []);

  async function refresh() {
    await load();
    setNewEvents(false);
  }

  return (
    <div className="card">
      <div className="section-header">
        <span className="card-title">
          Experiment Runs
          {newEvents && (
            <span
              title="New activity on the registry"
              style={{ marginLeft: 8, display: "inline-block", width: 8, height: 8,
                       borderRadius: 9999, background: "#4fd1c5" }}
            />
          )}
        </span>
        <button className="btn" onClick={refresh} style={{ fontSize: 12 }}>Refresh</button>
      </div>

      {error && <div className="empty-state">⚠ {error}</div>}
      {!error && runs === null && <div className="empty-state">Loading…</div>}
      {!error && runs?.length === 0 && (
        <div className="empty-state">
          No runs yet — start one with <code>av run start</code>, or from the Python SDK
          via <code>Repo.run_start()</code>.
        </div>
      )}

      {runs && runs.length > 0 && (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ color: "var(--text-muted)", textAlign: "left" }}>
              <th style={{ padding: "6px 8px" }}>Status</th>
              <th style={{ padding: "6px 8px" }}>Run</th>
              <th style={{ padding: "6px 8px" }}>Name</th>
              <th style={{ padding: "6px 8px" }}>By</th>
              <th style={{ padding: "6px 8px" }}>Metrics</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id} style={{ borderTop: "1px solid var(--border)" }}>
                <td style={{ padding: "6px 8px", color: STATUS_COLORS[r.status] ?? "inherit" }}>
                  ● {r.status}
                </td>
                <td style={{ padding: "6px 8px", fontFamily: "monospace" }}>{r.id.slice(0, 8)}</td>
                <td style={{ padding: "6px 8px" }}>{r.name ?? "—"}</td>
                <td style={{ padding: "6px 8px", color: "var(--text-muted)" }}>
                  {r.created_by ?? r.created_at?.slice(0, 16)?.replace("T", " ") ?? ""}
                </td>
                <td style={{ padding: "6px 8px" }}>
                  {Object.entries(r.metrics_summary || {}).slice(0, 4).map(([k, v]) => (
                    <span key={k} style={{ marginRight: 10 }}>
                      {k}=<strong>{String(v)}</strong>
                    </span>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
