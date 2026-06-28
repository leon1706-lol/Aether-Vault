"use client";

import { useEffect, useMemo, useState } from "react";
import { fetchCommit, formatBytes, type Commit, type StorageStats } from "@/lib/api";

interface Props {
  stats: StorageStats | null;
  commits: Commit[];
  loading: boolean;
  projectId?: string | null;
}

const TOP_N = 12;

export function StoragePanel({ stats, commits, loading }: Props) {
  const [latestDetail, setLatestDetail] = useState<Commit | null>(null);
  const [detailLoading, setDetailLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // commits is returned newest-first (see lib/api.ts fetchCommits), so [0] is the latest.
  const latestHash = commits[0]?.hash ?? null;

  useEffect(() => {
    if (!latestHash) {
      setDetailLoading(false);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    fetchCommit(latestHash)
      .then((d) => {
        if (!cancelled) setLatestDetail(d);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load snapshot detail");
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [latestHash]);

  const { byExtension, largest, distinctHashCount } = useMemo(() => {
    const tree = latestDetail?.tree ?? {};
    const buckets = new Map<string, { count: number; bytes: number }>();
    const files: { path: string; size: number; hash: string }[] = [];
    const hashes = new Set<string>();

    for (const [path, entry] of Object.entries(tree)) {
      const ext = path.includes(".") ? path.slice(path.lastIndexOf(".") + 1).toLowerCase() : "(none)";
      const bucket = buckets.get(ext) ?? { count: 0, bytes: 0 };
      bucket.count += 1;
      bucket.bytes += entry.size;
      buckets.set(ext, bucket);
      hashes.add(entry.hash);

      if (entry.layers && entry.layers.length > 0) {
        for (const layer of entry.layers) {
          files.push({ path: `${path} :: ${layer.name}`, size: layer.size, hash: layer.hash });
          hashes.add(layer.hash);
        }
      } else {
        files.push({ path, size: entry.size, hash: entry.hash });
      }
    }

    const byExtension = [...buckets.entries()]
      .map(([ext, v]) => ({ ext, ...v }))
      .sort((a, b) => b.bytes - a.bytes);
    const largest = [...files].sort((a, b) => b.size - a.size).slice(0, TOP_N);

    return { byExtension, largest, distinctHashCount: hashes.size };
  }, [latestDetail]);

  const totalObjects = stats?.total_objects ?? stats?.object_count ?? 0;
  // Approximate, not exact: total_objects is deduplicated across every project sharing this
  // registry, while distinctHashCount only covers the latest snapshot's tracked files.
  const dedupRatio =
    distinctHashCount > 0 && totalObjects > 0 ? (totalObjects / distinctHashCount).toFixed(2) : null;

  if (loading && !stats) {
    return (
      <div className="card">
        <div className="loading-overlay">
          <div className="spinner" />
          Loading storage stats…
        </div>
      </div>
    );
  }

  return (
    <div className="section fade-in fade-in-1">
      <div className="stats-grid section">
        <div className="stat-card">
          <div className="stat-label">CAS Objects (store-wide)</div>
          <div className="stat-value accent-orange">{totalObjects.toLocaleString()}</div>
          <div className="stat-sub">deduplicated across all projects</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total Size (store-wide)</div>
          <div className="stat-value">{formatBytes(stats?.total_size_bytes ?? 0)}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Approx. Dedup Ratio</div>
          <div className="stat-value accent-orange-soft">{dedupRatio ?? "—"}</div>
          <div className="stat-sub">store objects ÷ latest snapshot&apos;s distinct hashes</div>
        </div>
      </div>

      <div className="diff-truncate-notice">
        The breakdown and largest-files list below describe only the <strong>latest commit&apos;s
        tracked files</strong> — not every object in the CAS store. A true store-wide breakdown,
        growth-over-time, and a store-wide largest-objects list all need new backend endpoints
        (no path/extension metadata on stored objects, no historical snapshots, no listable
        object table today) and are not built here.
      </div>

      {error && <div className="diff-warning">{error}</div>}

      {detailLoading ? (
        <div className="loading-overlay">
          <div className="spinner" />
          Loading latest snapshot…
        </div>
      ) : !latestDetail || byExtension.length === 0 ? (
        <div className="empty-state">No tracked files in the latest commit.</div>
      ) : (
        <div className="grid-2">
          <div className="card">
            <div className="section-header">
              <span className="card-title">File-Type Breakdown</span>
              <span className="section-count">{byExtension.length} types</span>
            </div>
            <div className="metric-row" style={{ fontWeight: 600, color: "var(--text-muted)" }}>
              <span>Extension</span>
              <span>Files · Size</span>
            </div>
            {byExtension.map((b) => (
              <div key={b.ext} className="metric-row">
                <span className="metric-key">.{b.ext}</span>
                <span className="metric-val">{b.count} · {formatBytes(b.bytes)}</span>
              </div>
            ))}
          </div>

          <div className="card">
            <div className="section-header">
              <span className="card-title">Largest Tracked Files</span>
              <span className="section-count">top {largest.length}</span>
            </div>
            {largest.map((f) => (
              <div key={f.path} className="metric-row">
                <span className="metric-key" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 220 }}>
                  {f.path}
                </span>
                <span className="metric-val">{formatBytes(f.size)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
