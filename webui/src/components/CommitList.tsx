"use client";

import { shortHash, type Commit } from "@/lib/api";

interface Props {
  commits: Commit[];
  loading: boolean;
  error?: string | null;
}

export function CommitList({ commits, loading, error }: Props) {
  if (loading && commits.length === 0) {
    return (
      <div className="card">
        <div className="card-header">
          <span className="card-title">
            <CommitIcon />
            Commit Log
          </span>
        </div>
        <div className="loading-overlay">
          <div className="spinner" />
          Loading commits…
        </div>
      </div>
    );
  }

  if (error && commits.length === 0) {
    return (
      <div className="card">
        <div className="card-header">
          <span className="card-title">
            <CommitIcon />
            Commit Log
          </span>
        </div>
        <div className="empty-state">⚠ {error}</div>
      </div>
    );
  }

  if (commits.length === 0) {
    return (
      <div className="card">
        <div className="card-header">
          <span className="card-title">
            <CommitIcon />
            Commit Log
          </span>
        </div>
        <div className="empty-state">
          <CommitIcon size={32} />
          <span>No commits yet</span>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            Push your first commit with{" "}
            <code style={{ fontSize: 11 }}>av commit -m &quot;…&quot;</code>
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="section-header">
        <span className="card-title">
          <CommitIcon />
          Commit Log
        </span>
        <span className="section-count">{commits.length}</span>
      </div>

      <div
        className="commit-list"
        style={{ maxHeight: 400, overflowY: "auto" }}
      >
        {commits.map((commit, idx) => (
          <CommitRow key={commit.hash} commit={commit} isLast={idx === commits.length - 1} />
        ))}
      </div>
    </div>
  );
}

function CommitRow({ commit, isLast }: { commit: Commit; isLast: boolean }) {
  const time = commit.timestamp
    ? formatRelative(new Date(commit.timestamp))
    : "unknown";

  const metricEntries = Object.entries(commit.metrics ?? {});

  return (
    <div className="commit-item">
      <div className="commit-dot-col">
        <div className="commit-dot" />
        {!isLast && <div className="commit-line" />}
      </div>

      <div className="commit-body">
        <div className="commit-message" title={commit.message}>
          {commit.message}
        </div>
        <div className="commit-meta">
          <span className="commit-hash">{shortHash(commit.hash)}</span>
          <span className="commit-author">{commit.author}</span>
          <span className="commit-time">{time}</span>
        </div>

        {(commit.tags?.length > 0 || metricEntries.length > 0) && (
          <div className="commit-badges">
            {commit.tags?.map((t) => (
              <span key={t} className="tag-pill tag">
                🏷 {t}
              </span>
            ))}
            {metricEntries.slice(0, 4).map(([k, v]) => (
              <span key={k} className="tag-pill metric">
                {k}={typeof v === "number" ? v.toFixed(2) : v}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function formatRelative(date: Date): string {
  const now = Date.now();
  const diff = now - date.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return date.toLocaleDateString();
}

function CommitIcon({ size = 16 }: { size?: number }) {
  return (
    <svg
      width={size} height={size}
      viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="4" />
      <line x1="1.05" y1="12" x2="7" y2="12" />
      <line x1="17.01" y1="12" x2="22.96" y2="12" />
    </svg>
  );
}
