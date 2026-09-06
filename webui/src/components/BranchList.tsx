"use client";

import { shortHash, type Commit, type Ref } from "@/lib/api";

interface Props {
  refs: Ref;
  commits: Commit[];
  loading: boolean;
  error?: string | null;
}

export function BranchList({ refs, commits, loading, error }: Props) {
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

  // v1.3.0: distinguishes "the registry is unreachable/errored" from "genuinely no
  // branches yet" — these used to render identically (see development/Probleme.md).
  if (error && branches.length === 0) {
    return (
      <div className="card">
        <div className="card-header">
          <span className="card-title">
            <BranchIcon />
            Branches
          </span>
        </div>
        <div className="empty-state">⚠ {error}</div>
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
          // Refs are namespaced "<project_id>/<branch>" server-side so two projects can
          // each have a "main" branch without colliding; prefix with the project name
          // when refs from multiple projects are shown together (no project selected).
          const slashIdx = name.indexOf("/");
          const branchName = slashIdx === -1 ? name : name.slice(slashIdx + 1);
          const displayName = tip?.project_name ? `${tip.project_name} / ${branchName}` : branchName;
          return (
            <div key={name} className="branch-item">
              <div>
                <div className="branch-name">
                  <BranchIcon size={14} />
                  {displayName}
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
      style={{ color: "var(--accent-orange-soft)", flexShrink: 0 }}
    >
      <line x1="6" y1="3" x2="6" y2="15" />
      <circle cx="18" cy="6" r="3" />
      <circle cx="6" cy="18" r="3" />
      <path d="M18 9a9 9 0 01-9 9" />
    </svg>
  );
}
