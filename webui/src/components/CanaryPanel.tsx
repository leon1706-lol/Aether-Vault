"use client";

import { useEffect, useState } from "react";
import { fetchCanaryResults, type CanaryResult } from "@/lib/api";

interface Props {
  projectId: string | null;
  /** Test seam / embedding hint: caps how many recent results are fetched and shown. */
  limit?: number;
}

// v1.3.1 (RSI R6, WP-38): canary status + trend, the WebUI counterpart of
// `av canary status`/`av canary run` — see development/architecture.md's "Capability
// Canary Contract". Embedded inside the Regression tab rather than its own top-level
// nav entry (see page.tsx) — a standalone component either way, matching the plan's
// naming, just not a separate sidebar destination for one small status widget.
export function CanaryPanel({ projectId, limit = 20 }: Props) {
  const [results, setResults] = useState<CanaryResult[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchCanaryResults({ projectId, limit })
      .then((rows) => {
        if (!cancelled) {
          setResults(rows);
          setError(null);
        }
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
  }, [projectId, limit]);

  const passed = results.filter((r) => r.passed).length;
  const total = results.length;

  return (
    <div className="card">
      <div className="section-header">
        <span className="card-title">
          <CanaryIcon />
          Capability Canaries
        </span>
        {!loading && !error && (
          <span className="section-count" title="Passed / total, most recent results">
            {passed}/{total}
          </span>
        )}
      </div>
      {loading ? (
        <div className="loading-overlay">
          <div className="spinner" />
          Loading canary results…
        </div>
      ) : error ? (
        <div className="empty-state">Failed to load: {error}</div>
      ) : results.length === 0 ? (
        <div className="empty-state">
          <CanaryIcon size={32} />
          <span>No canary results recorded yet</span>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            Run <code style={{ fontSize: 11 }}>av canary run NAME</code> against an
            improver version to record one.
          </span>
        </div>
      ) : (
        <>
          <div style={{ display: "flex", gap: 3, padding: "4px 8px 12px" }} title="Most recent first, left to right">
            {[...results].reverse().map((r) => (
              <span
                key={r.id}
                title={`${r.passed ? "PASS" : "FAIL"} — improver ${r.improver_id.slice(0, 8)} — ${r.created_at ?? ""}`}
                style={{
                  width: 10, height: 20, borderRadius: 2,
                  background: r.passed ? "#68d391" : "#fc8181",
                }}
              />
            ))}
          </div>
          <div className="checkpoint-list">
            {results.map((r) => (
              <div key={r.id} className="checkpoint-row" style={{ padding: "10px 8px" }}>
                <span
                  className="checkpoint-iter"
                  style={{ color: r.passed ? "#68d391" : "#fc8181" }}
                >
                  {r.passed ? "PASS" : "FAIL"}
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, color: "var(--text-primary)" }}>
                    improver {r.improver_id.slice(0, 8)}
                  </div>
                  <div style={{ fontSize: 11.5, color: "var(--text-secondary)" }}>
                    {r.created_at ? new Date(r.created_at).toLocaleString() : "—"}
                    {r.run_id ? ` · run ${r.run_id.slice(0, 8)}` : ""}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function CanaryIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2a5 5 0 00-5 5v3a5 5 0 0010 0V7a5 5 0 00-5-5z" />
      <path d="M8 14v2a4 4 0 008 0v-2" />
      <line x1="12" y1="20" x2="12" y2="22" />
      <line x1="8" y1="22" x2="16" y2="22" />
    </svg>
  );
}
