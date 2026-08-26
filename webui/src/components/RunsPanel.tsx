"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchCommit,
  fetchLatestEventId,
  fetchRun,
  fetchRuns,
  type Commit,
  type Run,
} from "@/lib/api";
import {
  commitMetricsRows,
  lineageChain,
  metricColumns,
  summarizeTreeDiff,
  type TreeDiffSummary,
} from "@/lib/runDetail";

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

// Cap the per-run fan-out: a long run can link hundreds of commits — the detail view
// shows the most recent window (the registry cursor /api/events covers "everything").
const MAX_DETAIL_COMMITS = 20;

interface RunDetail {
  run: Run;
  lineage: Run[];
  commits: Commit[];
  summary: TreeDiffSummary | null;
}

// Runs tab (v1.2.0 list; v1.2.2 expandable detail): first-class experiment grouping.
// Rows expand into a detail panel — parent lineage chain, linked commits with messages
// and metrics, and a semantic summary composed CLIENT-SIDE from the last two linked
// commits' trees (no new server endpoint). The live badge in the section header is fed
// by the event-stream cursor: an agent pushing elsewhere makes it pulse without any
// manual refresh.
export function RunsPanel({ projectId, runsPollMs = 15_000, eventsPollMs = 10_000 }: Props) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [newEvents, setNewEvents] = useState(false);
  const lastEventId = useRef(0);

  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

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
  }, [load, runsPollMs]);

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

  async function toggleRow(runId: string) {
    if (expandedId === runId) {
      setExpandedId(null);
      setDetail(null);
      return;
    }
    setExpandedId(runId);
    setDetail(null);
    setDetailError(null);
    setDetailLoading(true);
    try {
      const run = await fetchRun(runId);
      // Lineage beyond the loaded page: walk parent_run_id via single-run fetches
      // (cycle-guarded + depth-capped inside lineageChain's caller loop).
      const lineageRuns: Run[] = [run];
      let cursor = run.parent_run_id;
      let depth = 0;
      while (cursor && depth < 10) {
        try {
          const parent = await fetchRun(cursor);
          lineageRuns.push(parent);
          cursor = parent.parent_run_id;
        } catch {
          break; // missing/unknown ancestor: show what we have, honestly
        }
        depth++;
      }

      const hashes = (run.commit_hashes ?? []).slice(0, MAX_DETAIL_COMMITS);
      const commits: Commit[] = [];
      for (const h of hashes) {
        try {
          commits.push(await fetchCommit(h));
        } catch {
          /* individual commit read failure must not kill the whole detail */
        }
      }
      commits.sort((a, b) => (b.timestamp ?? "").localeCompare(a.timestamp ?? ""));

      const summary =
        commits.length >= 1
          ? summarizeTreeDiff(commits[1]?.tree ?? {}, commits[0].tree ?? {})
          : null;

      setDetail({ run, lineage: lineageChain(lineageRuns, run.id), commits, summary });
    } catch (e) {
      setDetailError(e instanceof Error ? e.message : String(e));
    } finally {
      setDetailLoading(false);
    }
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
              <RunRow
                key={r.id}
                run={r}
                expanded={expandedId === r.id}
                onToggle={() => toggleRow(r.id)}
                detail={expandedId === r.id ? detail : null}
                loading={expandedId === r.id && detailLoading}
                error={expandedId === r.id ? detailError : null}
              />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function RunRow({
  run,
  expanded,
  onToggle,
  detail,
  loading,
  error,
}: {
  run: Run;
  expanded: boolean;
  onToggle: () => void;
  detail: RunDetail | null;
  loading: boolean;
  error: string | null;
}) {
  return (
    <>
      <tr
        data-testid={`run-row-${run.id.slice(0, 8)}`}
        onClick={onToggle}
        style={{ borderTop: "1px solid var(--border)", cursor: "pointer" }}
        title="Click to expand run detail"
      >
        <td style={{ padding: "6px 8px", color: "var(--text-muted)", width: 20 }}>
          {expanded ? "▾" : "▸"}
        </td>
        <td style={{ padding: "6px 8px", color: STATUS_COLORS[run.status] ?? "inherit" }}>
          ● {run.status}
        </td>
        <td style={{ padding: "6px 8px", fontFamily: "monospace" }}>{run.id.slice(0, 8)}</td>
        <td style={{ padding: "6px 8px" }}>{run.name ?? "—"}</td>
        <td style={{ padding: "6px 8px", color: "var(--text-muted)" }}>
          {run.created_by ?? run.created_at?.slice(0, 16)?.replace("T", " ") ?? ""}
        </td>
        <td style={{ padding: "6px 8px" }}>
          {Object.entries(run.metrics_summary || {}).slice(0, 4).map(([k, v]) => (
            <span key={k} style={{ marginRight: 10 }}>
              {k}=<strong>{String(v)}</strong>
            </span>
          ))}
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={6} style={{ padding: "10px 14px", background: "var(--bg-elev, rgba(255,255,255,0.03))" }}>
            {loading && <div className="empty-state">Loading run detail…</div>}
            {!loading && error && <div className="empty-state">⚠ {error}</div>}
            {!loading && !error && detail && <RunDetailView detail={detail} />}
          </td>
        </tr>
      )}
    </>
  );
}

export function RunDetailView({ detail }: { detail: RunDetail }) {
  const rows = commitMetricsRows(detail.commits);
  const cols = metricColumns(rows);
  const s = detail.summary;
  return (
    <div style={{ display: "grid", gap: 12 }}>
      {/* Lineage chain (parent_run_id walk, self → root) */}
      <div>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>Lineage</div>
        {detail.lineage.length <= 1 ? (
          <span style={{ color: "var(--text-muted)" }}>no parent run</span>
        ) : (
          <div style={{ fontFamily: "monospace", fontSize: 12 }}>
            {detail.lineage.map((r, i) => (
              <div key={r.id}>
                {"  ".repeat(i)}↳ {r.id.slice(0, 8)}
                {r.name ? ` (${r.name})` : ""} [{r.status}]
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Context notes pointer (notes live in .avh context memory, not the registry) */}
      {detail.run.env_snapshot_id && (
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          env snapshot:{" "}
          <code>{detail.run.env_snapshot_id.slice(0, 16)}…</code>{" "}
          (<code>av replay {detail.run.id}</code>)
        </div>
      )}

      {/* Linked commits w/ messages + metrics table */}
      <div>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>
          Linked commits ({detail.run.commit_hashes?.length ?? 0} total
          {rows.length < (detail.run.commit_hashes?.length ?? 0)
            ? `, showing latest ${rows.length}` : ""})
        </div>
        {rows.length === 0 ? (
          <span style={{ color: "var(--text-muted)" }}>no commits linked yet</span>
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

      {/* Semantic summary from the last two linked commits' trees (client-side) */}
      <div>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>Latest change (semantic)</div>
        {!s ? (
          <span style={{ color: "var(--text-muted)" }}>
            needs at least one linked commit with tree data
          </span>
        ) : (
          <div style={{ fontSize: 12 }}>
            {s.added.length} added · {s.changed.length} changed · {s.removed.length} removed ·{" "}
            bytes {s.bytesBefore} → {s.bytesAfter}
            {(s.chunksReused || s.chunksNew) > 0 && (
              <>
                {" "}· chunks reused {s.chunksReused}/{s.chunksReused + s.chunksNew}
                {s.dedupEfficiency !== null &&
                  ` (dedup ${(s.dedupEfficiency * 100).toFixed(1)}%)`}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
