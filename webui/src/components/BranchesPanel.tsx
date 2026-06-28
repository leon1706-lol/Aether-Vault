"use client";

import { useMemo, useState } from "react";
import { createRef, shortHash, type Commit, type Ref } from "@/lib/api";
import { commitsAhead, indexByHash, reachableFromTip } from "@/lib/branchGraph";

interface Props {
  refs: Ref;
  commits: Commit[];
  loading: boolean;
  projectId?: string | null;
}

// "main" is the conventional base branch name within a project namespace (project_id/main) —
// used as the comparison base for "commits ahead." If a project has no ref literally named
// main (e.g. "<project>/main"), ahead-counts are simply not shown for that project's branches.
function baseRefNameFor(branchRefName: string, refs: Ref): string | null {
  const slashIdx = branchRefName.indexOf("/");
  if (slashIdx === -1) return refs["main"] !== undefined ? "main" : null;
  const prefix = branchRefName.slice(0, slashIdx);
  const candidate = `${prefix}/main`;
  return refs[candidate] !== undefined ? candidate : null;
}

export function BranchesPanel({ refs, commits, loading, projectId }: Props) {
  const commitByHash = useMemo(() => indexByHash(commits), [commits]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [creating, setCreating] = useState<{ name: string; fromHash: string } | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const branches = Object.entries(refs);

  if (loading && branches.length === 0) {
    return (
      <div className="card">
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
        <div className="empty-state">
          <span>No branches found</span>
        </div>
      </div>
    );
  }

  async function handleCreate() {
    if (!creating || !creating.name.trim()) return;
    setBusy(true);
    setCreateError(null);
    try {
      const prefix = projectId ? `${projectId}/` : "";
      await createRef(`${prefix}${creating.name.trim()}`, creating.fromHash);
      setCreating(null);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Failed to create branch");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card section fade-in fade-in-1">
      <div className="section-header">
        <span className="card-title">Branches</span>
        <span className="section-count">{branches.length}</span>
      </div>

      {branches.map(([name, tipHash]) => {
        const tip = commitByHash.get(tipHash);
        const slashIdx = name.indexOf("/");
        const branchName = slashIdx === -1 ? name : name.slice(slashIdx + 1);
        const displayName = tip?.project_name ? `${tip.project_name} / ${branchName}` : branchName;
        const isExpanded = expanded === name;

        const baseRefName = baseRefNameFor(name, refs);
        const ahead =
          baseRefName && baseRefName !== name
            ? commitsAhead(tipHash, refs[baseRefName], commitByHash)
            : null;

        const { hashes: branchHashes } = reachableFromTip(tipHash, commitByHash);
        const branchCommits = commits
          .filter((c) => branchHashes.has(c.hash))
          .sort((a, b) => {
            const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
            const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
            return tb - ta;
          });

        return (
          <div key={name} className="branch-item" style={{ flexDirection: "column", alignItems: "stretch" }}>
            <div
              style={{ display: "flex", alignItems: "center", justifyContent: "space-between", cursor: "pointer" }}
              onClick={() => setExpanded(isExpanded ? null : name)}
            >
              <div>
                <div className="branch-name">{displayName}</div>
                {tip && (
                  <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 3 }}>
                    {tip.message}
                  </div>
                )}
                <div style={{ fontSize: 11.5, color: "var(--text-secondary)", marginTop: 4 }}>
                  {tip?.author ?? "—"}
                  {tip?.timestamp ? ` · ${new Date(tip.timestamp).toLocaleString()}` : ""}
                  {ahead && (
                    <>
                      {" · "}
                      <span style={{ color: "var(--accent-orange-soft)" }}>
                        {ahead.count} ahead of {baseRefName}
                        {ahead.truncated ? " (of loaded history)" : ""}
                      </span>
                    </>
                  )}
                </div>
              </div>
              <div style={{ textAlign: "right", flexShrink: 0, display: "flex", alignItems: "center", gap: 10 }}>
                <div className="branch-tip">{shortHash(tipHash)}</div>
                <button
                  type="button"
                  className="btn btn-ghost"
                  style={{ padding: "4px 10px" }}
                  onClick={(e) => {
                    e.stopPropagation();
                    setCreating({ name: "", fromHash: tipHash });
                    setCreateError(null);
                  }}
                >
                  Branch from here
                </button>
              </div>
            </div>

            {isExpanded && (
              <div style={{ marginTop: 10, paddingLeft: 4 }}>
                {branchCommits.length === 0 ? (
                  <div className="empty-state" style={{ padding: "12px 0" }}>
                    No commits reachable in the loaded history window.
                  </div>
                ) : (
                  <div className="commit-list">
                    {branchCommits.map((c) => (
                      <div key={c.hash} className="commit-item">
                        <div className="commit-body">
                          <div className="commit-message" title={c.message}>{c.message}</div>
                          <div className="commit-meta">
                            <span className="commit-hash">{shortHash(c.hash)}</span>
                            <span className="commit-author">{c.author}</span>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}

      {creating && (
        <div className="card" style={{ marginTop: 16, background: "var(--bg-card)" }}>
          <div className="card-title" style={{ marginBottom: 10 }}>
            New branch from {shortHash(creating.fromHash)}
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              type="text"
              placeholder="branch-name"
              value={creating.name}
              onChange={(e) => setCreating({ ...creating, name: e.target.value })}
              style={{
                flex: 1,
                background: "var(--bg-deep)",
                border: "1px solid var(--border)",
                color: "var(--text-primary)",
                borderRadius: "var(--radius-sm)",
                padding: "6px 10px",
                fontSize: 13,
              }}
            />
            <button type="button" className="btn btn-primary" disabled={busy} onClick={handleCreate}>
              {busy ? "Creating…" : "Create"}
            </button>
            <button type="button" className="btn btn-ghost" onClick={() => setCreating(null)}>
              Cancel
            </button>
          </div>
          {createError && (
            <div className="diff-warning" style={{ marginTop: 10 }}>
              {createError}
            </div>
          )}
        </div>
      )}

      <div className="diff-truncate-notice" style={{ marginTop: 14 }}>
        Branch delete isn&apos;t available yet — the server has no DELETE endpoint for refs.
      </div>
    </div>
  );
}
