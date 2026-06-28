"use client";

import { useEffect, useMemo, useState } from "react";
import { fetchCommit, fetchCommitsPage, shortHash, type Commit, type Ref } from "@/lib/api";
import { indexByHash, reachableFromTip } from "@/lib/branchGraph";

interface Props {
  refs: Ref;
  projectId?: string | null;
}

const PAGE_SIZE = 50;

export function CommitsPanel({ refs, projectId }: Props) {
  const [commits, setCommits] = useState<Commit[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [branchFilter, setBranchFilter] = useState("__all__");
  const [expandedHash, setExpandedHash] = useState<string | null>(null);
  const [detailCache, setDetailCache] = useState<Map<string, Commit>>(new Map());
  const [detailLoading, setDetailLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setCommits([]);
    setOffset(0);
    setHasMore(true);
    fetchCommitsPage(PAGE_SIZE, 0, projectId)
      .then((page) => {
        if (cancelled) return;
        setCommits(page.commits ?? []);
        setOffset(PAGE_SIZE);
        setHasMore(page.next_offset !== null);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load commits");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  async function loadMore() {
    setLoadingMore(true);
    try {
      const page = await fetchCommitsPage(PAGE_SIZE, offset, projectId);
      setCommits((prev) => [...prev, ...(page.commits ?? [])]);
      setOffset(offset + PAGE_SIZE);
      setHasMore(page.next_offset !== null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load more commits");
    } finally {
      setLoadingMore(false);
    }
  }

  const commitByHash = useMemo(() => indexByHash(commits), [commits]);

  const branchScoped = useMemo(() => {
    if (branchFilter === "__all__") return commits;
    const { hashes } = reachableFromTip(refs[branchFilter], commitByHash);
    return commits.filter((c) => hashes.has(c.hash));
  }, [branchFilter, commits, refs, commitByHash]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return branchScoped;
    return branchScoped.filter(
      (c) =>
        c.message.toLowerCase().includes(q) ||
        c.author.toLowerCase().includes(q) ||
        c.tags?.some((t) => t.toLowerCase().includes(q))
    );
  }, [branchScoped, search]);

  async function toggleExpand(hash: string) {
    if (expandedHash === hash) {
      setExpandedHash(null);
      return;
    }
    setExpandedHash(hash);
    if (detailCache.has(hash)) return;
    setDetailLoading(true);
    try {
      const commit = commitByHash.get(hash);
      const fetches = [fetchCommit(hash)];
      if (commit?.parent_hash) fetches.push(fetchCommit(commit.parent_hash));
      const [detail, parentDetail] = await Promise.all(fetches);
      setDetailCache((prev) => {
        const next = new Map(prev);
        next.set(hash, detail);
        if (parentDetail) next.set(parentDetail.hash, parentDetail);
        return next;
      });
    } catch {
      // leave uncached — row stays collapsed-detail with an inline failure note
    } finally {
      setDetailLoading(false);
    }
  }

  if (loading) {
    return (
      <div className="card">
        <div className="loading-overlay">
          <div className="spinner" />
          Loading commits…
        </div>
      </div>
    );
  }

  return (
    <div className="card section fade-in fade-in-1">
      <div className="section-header">
        <span className="card-title">Commits</span>
        <span className="section-count">{filtered.length} loaded</span>
      </div>

      <div style={{ display: "flex", gap: 10, marginBottom: 14, flexWrap: "wrap" }}>
        <input
          type="text"
          placeholder="Search loaded commits by message, author, or tag…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{
            flex: 1,
            minWidth: 220,
            background: "var(--bg-card)",
            border: "1px solid var(--border)",
            color: "var(--text-primary)",
            borderRadius: "var(--radius-sm)",
            padding: "6px 10px",
            fontSize: 13,
          }}
        />
        <select value={branchFilter} onChange={(e) => setBranchFilter(e.target.value)}>
          <option value="__all__">All branches</option>
          {Object.keys(refs).map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </div>

      {error && <div className="diff-warning">{error}</div>}

      <div className="diff-truncate-notice">
        Search and branch filtering apply only to the {commits.length} commits loaded so far —
        not full project history.
      </div>

      <div className="commit-list">
        {filtered.map((commit) => {
          const isExpanded = expandedHash === commit.hash;
          const detail = detailCache.get(commit.hash);
          const parentDetail = commit.parent_hash ? detailCache.get(commit.parent_hash) : undefined;

          return (
            <div key={commit.hash}>
              <div
                className="commit-item"
                style={{ cursor: "pointer" }}
                onClick={() => toggleExpand(commit.hash)}
              >
                <div className="commit-dot-col">
                  <div className="commit-dot" />
                </div>
                <div className="commit-body">
                  <div className="commit-message" title={commit.message}>
                    {commit.message}
                  </div>
                  <div className="commit-meta">
                    <span className="commit-hash">{shortHash(commit.hash)}</span>
                    <span className="commit-author">{commit.author}</span>
                    <span className="commit-time">
                      {commit.timestamp ? new Date(commit.timestamp).toLocaleString() : "unknown"}
                    </span>
                  </div>
                  {(commit.tags?.length > 0 || Object.keys(commit.metrics ?? {}).length > 0) && (
                    <div className="commit-badges">
                      {commit.tags?.map((t) => (
                        <span key={t} className="tag-pill tag">🏷 {t}</span>
                      ))}
                      {Object.entries(commit.metrics ?? {}).slice(0, 4).map(([k, v]) => (
                        <span key={k} className="tag-pill metric">
                          {k}={typeof v === "number" ? v.toFixed(2) : v}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>

              {isExpanded && (
                <div style={{ paddingLeft: 24, paddingBottom: 14 }}>
                  {!detail ? (
                    <div className="loading-overlay" style={{ padding: "12px 0" }}>
                      {detailLoading ? (
                        <>
                          <div className="spinner" />
                          Loading file tree…
                        </>
                      ) : (
                        "Failed to load detail."
                      )}
                    </div>
                  ) : (
                    <FileTreeDiff detail={detail} parentDetail={parentDetail} />
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {hasMore && (
        <button
          type="button"
          className="btn btn-ghost"
          style={{ marginTop: 14 }}
          disabled={loadingMore}
          onClick={loadMore}
        >
          {loadingMore ? "Loading…" : "Load more"}
        </button>
      )}
    </div>
  );
}

function FileTreeDiff({ detail, parentDetail }: { detail: Commit; parentDetail?: Commit }) {
  const tree = detail.tree ?? {};
  const parentTree = parentDetail?.tree ?? {};
  const paths = [...new Set([...Object.keys(tree), ...Object.keys(parentTree)])].sort();

  if (paths.length === 0) {
    return <div className="empty-state" style={{ padding: "12px 0" }}>No tracked files.</div>;
  }

  return (
    <div className="diff-slots" style={{ marginBottom: 0 }}>
      {paths.map((path) => {
        const entry = tree[path];
        const parentEntry = parentTree[path];
        let status: "added" | "removed" | "changed" | "unchanged";
        if (!parentEntry) status = "added";
        else if (!entry) status = "removed";
        else if (entry.hash !== parentEntry.hash) status = "changed";
        else status = "unchanged";

        return (
          <div key={path} className="diff-slot diff-slot--filled">
            <div className="diff-slot-content">
              <span className={`tag-pill diff-status diff-status--${status}`}>{status}</span>
              <span className="checkpoint-path">{path}</span>
              <span className="checkpoint-hash">{(entry ?? parentEntry).size} bytes</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
