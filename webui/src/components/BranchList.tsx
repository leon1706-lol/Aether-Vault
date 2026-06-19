"use client";

import { shortHash, type Commit, type Ref } from "@/lib/api";

interface Props {
  refs: Ref;
  commits: Commit[];
  loading: boolean;
}

export function BranchList({ refs, commits, loading }: Props) {
  const commitByHash: Record<string, Commit> = {};
  for (const c of commits) {
    commitByHash[c.hash] = c;
  }

  const branches = Object.entries(refs);

  if (loading && branches.length === 0) {
    return (
      <div className="card">
        <div className="card-header">
          <span className="card-title">
            <BranchIcon />
            Branches
          </span>
        </div>
        <div className="loading-overlay">
          <div className="spinner" />
          Loading branches…
        </div>
      </div>
    );
  }

  if (branches.length === 0) {
    return (
      <div className="card">
        <div className="card-header">
          <span className="card-title">
            <BranchIcon />
            Branches
          </span>
        </div>
        <div className="empty-state">
          <BranchIcon size={32} />
          <span>No branches found</span>
        </div>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="section-header">
        <span className="card-title">
          <BranchIcon />
          Branches
        </span>
        <span className="section-count">{branches.length}</span>
      </div>

      <div style={{ maxHeight: 360, overflowY: "auto" }}>
        {branches.map(([name, tipHash]) => {
          const tip = commitByHash[tipHash];
          return (
            <div key={name} className="branch-item">
              <div>
                <div className="branch-name">
                  <BranchIcon size={14} />
                  {name}
                </div>
                {tip && (
                  <div
                    style={{
                      fontSize: 12,
                      color: "var(--text-muted)",
                      marginTop: 3,
                    }}
                  >
                    {tip.message.slice(0, 50)}
                    {tip.message.length > 50 ? "…" : ""}
                  </div>
                )}
              </div>

              <div style={{ textAlign: "right", flexShrink: 0 }}>
                <div className="branch-tip">{shortHash(tipHash)}</div>
                {tip?.timestamp && (
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                    {new Date(tip.timestamp).toLocaleDateString()}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function BranchIcon({ size = 16 }: { size?: number }) {
  return (
    <svg
      width={size} height={size}
      viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
      style={{ color: "var(--accent-purple)", flexShrink: 0 }}
    >
      <line x1="6" y1="3" x2="6" y2="15" />
      <circle cx="18" cy="6" r="3" />
      <circle cx="6" cy="18" r="3" />
      <path d="M18 9a9 9 0 01-9 9" />
    </svg>
  );
}
