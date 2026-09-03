"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchLatestEventId,
  fetchRunMetrics,
  fetchRunSummary,
  fetchRuns,
  type RunMetricPoint,
  type RunSummary,
  type Run,
} from "@/lib/api";
import { commitMetricsRows, metricColumns } from "@/lib/runDetail";
import { MetricsChart } from "@/components/MetricsChart";

const POLICY_BADGE_COLOR: Record<"allow" | "deny", string> = {
  allow: "#68d391",
  deny: "#fc8181",
};

interface Props {
  projectId: string | null;
  /** v1.2.5 deep linking: a run id to open immediately (from ?run=<id> in the URL). */
  initialRunId?: string | null;
  /** Test seam: poll intervals in ms (defaults match production cadence). */
  runsPollMs?: number;
  eventsPollMs?: number;
  /** v1.3.0 (todo.md item 25): cross-link into the weight-diff tab — see page.tsx. */
  onCompareWeights?: (olderHash: string, newerHash: string) => void;
}

const STATUS_COLORS: Record<Run["status"], string> = {
  created: "var(--text-muted)",
  running: "#4fd1c5",
  completed: "#68d391",
  failed: "#fc8181",
};

// Runs tab (v1.2.0 list; v1.2.2 expandable detail; v1.2.5 dedicated server-backed
// panel + deep linking): first-class experiment grouping. Clicking a row opens a
// dedicated detail panel below the table — lineage, linked commits, a metrics history
// chart, and a SERVER-COMPUTED semantic summary, all from ONE request
// (GET /api/runs/{id}/summary — replaces the old fetchRun()+N×fetchCommit() fan-out).
// The selected run id is kept in the URL (?run=<id>) so the panel is shareable/
// reloadable; the live badge in the header is fed by the event-stream cursor.
export function RunsPanel({
  projectId, initialRunId, runsPollMs = 15_000, eventsPollMs = 10_000, onCompareWeights,
}: Props) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newEvents, setNewEvents] = useState(false);
  const lastEventId = useRef(0);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const openedFromDeepLink = useRef(false);

  useEffect(() => {
    // fetchRunsPage below is defined with useCallback so this effect's own deps stay
    // stable; runs list load is independent of which run (if any) is selected.
    load();
    const id = setInterval(load, runsPollMs);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, runsPollMs]);

  useEffect(() => {
    if (initialRunId && !openedFromDeepLink.current) {
      openedFromDeepLink.current = true;
      selectRun(initialRunId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialRunId]);

  const load = useCallback(async () => {
    try {
      setRuns(await fetchRuns({ projectId, limit: 100 }));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId]);

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
  }, [eventsPollMs]);

  async function refresh() {
    await load();
    setNewEvents(false);
  }

  function selectRun(runId: string) {
    setSelectedId(runId);
    const params = new URLSearchParams(window.location.search);
    params.set("run", runId);
    window.history.replaceState(null, "", `?${params.toString()}`);
  }

  function closeDetail() {
    setSelectedId(null);
    const params = new URLSearchParams(window.location.search);
    params.delete("run");
    window.history.replaceState(null, "", `?${params.toString()}`);
  }

  function toggleRow(runId: string) {
    if (selectedId === runId) closeDetail();
    else selectRun(runId);
  }

  return (
    <div style={{ display: "grid", gap: 16 }}>
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
                <th style={{ padding: "6px 8px" }}></th>
                <th style={{ padding: "6px 8px" }}>Status</th>
                <th style={{ padding: "6px 8px" }}>Run</th>
                <th style={{ padding: "6px 8px" }}>Name</th>
                <th style={{ padding: "6px 8px" }}>By</th>
                <th style={{ padding: "6px 8px" }}>Metrics</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr
                  key={r.id}
                  data-testid={`run-row-${r.id.slice(0, 8)}`}
                  onClick={() => toggleRow(r.id)}
                  style={{
                    borderTop: "1px solid var(--border)", cursor: "pointer",
                    background: selectedId === r.id ? "var(--bg-elev, rgba(255,255,255,0.04))" : undefined,
                  }}
                  title="Click to open run detail"
                >
                  <td style={{ padding: "6px 8px", color: "var(--text-muted)", width: 20 }}>
                    {selectedId === r.id ? "▾" : "▸"}
                  </td>
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

      {selectedId && (
        <RunDetailPanel runId={selectedId} onClose={closeDetail} onCompareWeights={onCompareWeights} />
      )}
    </div>
  );
}

// v1.2.5: dedicated run-detail panel — fetches GET /api/runs/{id}/summary directly, so
// it renders correctly even for a deep-linked run id not present in the currently
// loaded runs page (e.g. an older run past the default limit=100 window).
function RunDetailPanel({
  runId, onClose, onCompareWeights,
}: {
  runId: string;
  onClose: () => void;
  /** v1.3.0 (todo.md item 25): cross-link into the weight-diff tab, pre-filling both
   * slots — see page.tsx's openWeightDiff(). Older commit first, newer second. */
  onCompareWeights?: (olderHash: string, newerHash: string) => void;
}) {
  const [summary, setSummary] = useState<RunSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // v1.3.0 (todo.md item 7): GET /api/runs/{id}/summary's inline `commits` is capped
  // (_RUN_SUMMARY_MAX_COMMITS) — this holds the FULL series from GET /api/runs/{id}/metrics
  // when there's more history than the cap shows, so the chart doesn't silently lose it.
  const [fullMetrics, setFullMetrics] = useState<RunMetricPoint[] | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setFullMetrics(null);
    try {
      setSummary(await fetchRunSummary(runId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!summary || summary.total_commits <= summary.commits.length) return;
    let cancelled = false;
    fetchRunMetrics(runId)
      .then((points) => { if (!cancelled) setFullMetrics(points); })
      .catch(() => { /* chart just falls back to the capped inline copy */ });
    return () => { cancelled = true; };
  }, [runId, summary]);

  const metricSource = fullMetrics ?? summary?.commits ?? [];
  const rows = commitMetricsRows(metricSource);
  const cols = metricColumns(rows);
  // A chart earns its place once there's real history to trend — a 2-point line is
  // barely more informative than the table and less scannable, so the table stays the
  // default until there's a genuine trend to show.
  const hasChartableMetrics =
    metricSource.filter((c) => Object.keys(c.metrics ?? {}).length > 0).length >= 3;

  const chronologicalCommits = fullMetrics
    ? [...fullMetrics].sort((a, b) => {
        const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
        const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
        return ta - tb;
      })
    : summary
    ? [...summary.commits].reverse() // summary.commits is newest-first
    : [];
  const latestTwo =
    chronologicalCommits.length >= 2
      ? [chronologicalCommits[chronologicalCommits.length - 2], chronologicalCommits[chronologicalCommits.length - 1]]
      : null;

  const policyOutcome = summary?.run.policy_outcome ?? null;

  return (
    <div className="card">
      <div className="section-header">
        <span className="card-title" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          Run detail — {runId.slice(0, 8)}…
          {policyOutcome && (
            <span
              className="tag-pill"
              style={{ color: POLICY_BADGE_COLOR[policyOutcome.decision], fontSize: 11 }}
              title={`policy ${policyOutcome.decision}${policyOutcome.rule ? ` (${policyOutcome.rule})` : ""} at ${policyOutcome.at}`}
            >
              policy: {policyOutcome.decision}
            </span>
          )}
        </span>
        <div style={{ display: "flex", gap: 8 }}>
          {onCompareWeights && latestTwo && (
            <button
              className="btn"
              style={{ fontSize: 12 }}
              onClick={() => onCompareWeights(latestTwo[0].hash, latestTwo[1].hash)}
            >
              Compare weights (latest 2)
            </button>
          )}
          <button className="btn" onClick={onClose} style={{ fontSize: 12 }}>Close</button>
        </div>
      </div>

      {loading && (
        <div className="loading-overlay">
          <div className="spinner" />
          Loading run detail…
        </div>
      )}
      {!loading && error && (
        <div className="empty-state">
          ⚠ {error}
          <div style={{ marginTop: 8 }}>
            <button className="btn" onClick={load} style={{ fontSize: 12 }}>Retry</button>
          </div>
        </div>
      )}
      {!loading && !error && summary && (
        <div style={{ display: "grid", gap: 14 }}>
          {/* Lineage chain (server-walked, self → root) */}
          <div>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>Lineage</div>
            {summary.lineage.length <= 1 ? (
              <span style={{ color: "var(--text-muted)" }}>no parent run</span>
            ) : (
              <div style={{ fontFamily: "monospace", fontSize: 12 }}>
                {summary.lineage.map((r, i) => (
                  <div key={r.id}>
                    {"  ".repeat(i)}↳ {r.id.slice(0, 8)}
                    {r.name ? ` (${r.name})` : ""} [{r.status}]
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Env snapshot + published context-memory pointers */}
          <div style={{ fontSize: 12, color: "var(--text-muted)", display: "grid", gap: 2 }}>
            {summary.env_snapshot_id ? (
              <div>
                env snapshot: <code>{summary.env_snapshot_id.slice(0, 16)}…</code>{" "}
                (<code>av replay {summary.run.id}</code>)
              </div>
            ) : (
              <div>no env snapshot linked to this run</div>
            )}
            {summary.avh_object_id ? (
              <div>
                context notes published — fetch the object at{" "}
                <code>{summary.avh_object_id.slice(0, 16)}…</code> for the full
                <code> .avh</code> (notes render inline once the object viewer ships)
              </div>
            ) : (
              <div>
                no published context notes — the repo owner can run{" "}
                <code>av handoff --publish</code> to attach them here
              </div>
            )}
          </div>

          {/* Linked commits: chart when there's a real history, table otherwise */}
          <div>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>
              Linked commits ({summary.total_commits} total
              {rows.length < summary.total_commits
                ? `, showing latest ${rows.length}`
                : fullMetrics
                ? " — full history"
                : ""}
              )
            </div>
            {rows.length === 0 ? (
              <span style={{ color: "var(--text-muted)" }}>no commits linked yet</span>
            ) : hasChartableMetrics ? (
              <MetricsChart
                commits={metricSource.map((c) => ({
                  hash: c.hash, message: c.message, author: "", timestamp: c.timestamp,
                  parent_hash: null, root_tree_hash: null, tags: [], metrics: c.metrics,
                }))}
                loading={false}
              />
            ) : (
              <table style={{ borderCollapse: "collapse", fontSize: 12 }}>
                <thead>
                  <tr style={{ color: "var(--text-muted)", textAlign: "left" }}>
                    <th style={{ padding: "3px 8px" }}>Commit</th>
                    <th style={{ padding: "3px 8px" }}>Message</th>
                    {cols.map((c) => (
                      <th key={c} style={{ padding: "3px 8px" }}>{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.hash}>
                      <td style={{ padding: "3px 8px", fontFamily: "monospace" }}>{r.short}</td>
                      <td style={{ padding: "3px 8px" }}>{r.message || "—"}</td>
                      {cols.map((c) => (
                        <td key={c} style={{ padding: "3px 8px" }}>
                          {r.metrics[c] !== undefined ? String(r.metrics[c]) : "—"}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* Server-computed semantic summary (v1.2.5 — was client-side only) */}
          <div>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>Latest change (semantic)</div>
            {!summary.semantic_summary ? (
              <span style={{ color: "var(--text-muted)" }}>
                needs at least two linked commits with tree data
              </span>
            ) : (
              <div style={{ fontSize: 12 }}>
                {summary.semantic_summary.summary} · bytes{" "}
                {summary.semantic_summary.totals.bytes_before} →{" "}
                {summary.semantic_summary.totals.bytes_after}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
