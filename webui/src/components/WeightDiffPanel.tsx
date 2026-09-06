"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { fetchCommit, fetchCommitsWithLayers, shortHash, type Commit } from "@/lib/api";
import { diffFile, unionModelPaths, type FileDiff } from "@/lib/diffWeights";
import { CheckpointPicker, type CheckpointRow } from "@/components/CheckpointPicker";
import { WeightHeatmap } from "@/components/WeightHeatmap";
import { LayerDriftChart } from "@/components/LayerDriftChart";

// How many recent commits to eagerly resolve into full trees so the checkpoint list can be
// built, via a single GET /api/commits?include_layers request. Kept at 100 to bound
// response size/render cost.
const CHECKPOINT_FETCH_LIMIT = 100;

interface Props {
  // When set, only checkpoints belonging to this project are offered for comparison.
  // When null, checkpoints from every project are shown.
  projectId?: string | null;
  // Shareable weight-diff link — read once from ?tab=weight-diff&a=<hash>&b=<hash>&path=<relPath>
  // by page.tsx and handed down. Applied once on mount.
  initialSlotAHash?: string | null;
  initialSlotBHash?: string | null;
  initialPath?: string | null;
}

function firstModelRelPath(commit: Commit): string | null {
  const tree = commit.tree ?? {};
  for (const [relPath, entry] of Object.entries(tree)) {
    if (entry.layers && entry.layers.length > 0) return relPath;
  }
  return null;
}

export function WeightDiffPanel({
  projectId = null, initialSlotAHash = null, initialSlotBHash = null, initialPath = null,
}: Props) {
  const [fullCommits, setFullCommits] = useState<Map<string, Commit>>(new Map());
  // Commits resolved by an explicit hash (arbitrary compare / deep link) — kept separate
  // from the eagerly-fetched "most recent" set so a projectId refetch can't drop a
  // manually-resolved older commit out from under an active selection.
  const [resolvedByHash, setResolvedByHash] = useState<Map<string, Commit>>(new Map());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hashLookupError, setHashLookupError] = useState<string | null>(null);
  const [slotA, setSlotA] = useState<CheckpointRow | null>(null);
  const [slotB, setSlotB] = useState<CheckpointRow | null>(null);
  const [activePath, setActivePath] = useState<string | null>(null);
  const deepLinkApplied = useRef(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setSlotA(null);
    setSlotB(null);
    (async () => {
      try {
        const details = await fetchCommitsWithLayers(CHECKPOINT_FETCH_LIMIT, projectId);
        if (cancelled) return;
        const map = new Map<string, Commit>();
        details.forEach((d) => map.set(d.hash, d));
        setFullCommits(map);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load checkpoints");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const allCommits = useMemo(() => {
    if (resolvedByHash.size === 0) return fullCommits;
    const merged = new Map(fullCommits);
    resolvedByHash.forEach((c, h) => merged.set(h, c));
    return merged;
  }, [fullCommits, resolvedByHash]);

  const rows: CheckpointRow[] = useMemo(() => {
    // Oldest → newest so iteration numbers ("v1, v2, …") increase with history.
    const ordered = [...allCommits.values()].sort((a, b) => {
      const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
      const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
      return ta - tb;
    });
    const out: CheckpointRow[] = [];
    ordered.forEach((commit, idx) => {
      const tree = commit.tree ?? {};
      for (const [relPath, entry] of Object.entries(tree)) {
        if (!entry.layers || entry.layers.length === 0) continue;
        out.push({
          iteration: idx + 1,
          commitHash: commit.hash,
          rel_path: relPath,
          weightHash: entry.hash,
        });
      }
    });
    return out;
  }, [allCommits]);

  // v1.3.0: syncs the URL (?a=&b=&path=) whenever the selection changes, so the current
  // comparison is always shareable/reloadable — mirrors RunsPanel's own ?run= convention.
  function syncUrl(a: CheckpointRow | null, b: CheckpointRow | null, path: string | null) {
    const params = new URLSearchParams(window.location.search);
    if (a) params.set("a", a.commitHash); else params.delete("a");
    if (b) params.set("b", b.commitHash); else params.delete("b");
    if (path) params.set("path", path); else params.delete("path");
    window.history.replaceState(null, "", `?${params.toString()}`);
  }

  function handleSlotChange(slot: "A" | "B", row: CheckpointRow | null) {
    const nextA = slot === "A" ? row : slotA;
    const nextB = slot === "B" ? row : slotB;
    if (slot === "A") setSlotA(row); else setSlotB(row);
    syncUrl(nextA, nextB, activePath ?? row?.rel_path ?? null);
  }

  // Resolves an arbitrary commit hash (fetching it if not already known) and fills the
  // given slot with its first model checkpoint or the deep-linked `preferredPath`.
  async function resolveAndFill(hash: string, slot: "A" | "B", preferredPath?: string | null) {
    const trimmed = hash.trim();
    if (!trimmed) return;
    let commit = allCommits.get(trimmed);
    if (!commit) {
      try {
        commit = await fetchCommit(trimmed);
      } catch (err) {
        setHashLookupError(
          err instanceof Error ? `${trimmed}: ${err.message}` : `Commit not found: ${trimmed}`
        );
        return;
      }
      setResolvedByHash((prev) => new Map(prev).set(commit!.hash, commit!));
    }
    const relPath =
      preferredPath && commit.tree?.[preferredPath]?.layers?.length
        ? preferredPath
        : firstModelRelPath(commit);
    if (!relPath) {
      setHashLookupError(`${shortHash(commit.hash)} has no model checkpoints.`);
      return;
    }
    const entry = commit.tree![relPath];
    // Best-effort chronological position among everything currently known — cosmetic
    // only ("v12"), so a snapshot taken before this commit's exact rank is confirmed by
    // the next full rows recomputation is an acceptable, self-correcting approximation.
    const iteration =
      [...allCommits.values(), commit]
        .sort((a, b) => {
          const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
          const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
          return ta - tb;
        })
        .findIndex((c) => c.hash === commit!.hash) + 1;
    setHashLookupError(null);
    handleSlotChange(slot, { iteration, commitHash: commit.hash, rel_path: relPath, weightHash: entry.hash });
  }

  // Applies the ?a=&b=&path= deep link exactly once, after the initial commit list load.
  // Waiting for `loading` to clear means recently-fetched commits fill in without a
  // network round trip; resolveAndFill() falls back to fetchCommit() for anything older.
  useEffect(() => {
    if (deepLinkApplied.current || loading) return;
    if (!initialSlotAHash && !initialSlotBHash) return;
    deepLinkApplied.current = true;
    (async () => {
      if (initialSlotAHash) await resolveAndFill(initialSlotAHash, "A", initialPath);
      if (initialSlotBHash) await resolveAndFill(initialSlotBHash, "B", initialPath);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, initialSlotAHash, initialSlotBHash, initialPath]);

  const fromTree = slotA ? allCommits.get(slotA.commitHash)?.tree : undefined;
  const toTree = slotB ? allCommits.get(slotB.commitHash)?.tree : undefined;

  const availablePaths = useMemo(() => unionModelPaths(fromTree, toTree), [fromTree, toTree]);

  // Default the compared path to whichever slot was filled most recently; fall back to the
  // first available path if that one no longer exists in either tree.
  const comparePath = useMemo(() => {
    if (activePath && availablePaths.includes(activePath)) return activePath;
    return slotB?.rel_path ?? slotA?.rel_path ?? availablePaths[0] ?? null;
  }, [activePath, availablePaths, slotA, slotB]);

  function handlePathChange(path: string) {
    setActivePath(path);
    syncUrl(slotA, slotB, path);
  }

  const fileDiff: FileDiff | null =
    slotA && slotB && comparePath ? diffFile(fromTree, toTree, comparePath) : null;

  const pathMismatch = !!(slotA && slotB && slotA.rel_path !== slotB.rel_path);

  return (
    <div className="weight-diff-layout">
      <div className="weight-diff-main">
        <div className="card section fade-in fade-in-1">
          <div className="section-header">
            <span className="card-title">Comprehensive Comparison</span>
            {fileDiff && (
              <span className={`tag-pill diff-status diff-status--${fileDiff.status}`}>
                {fileDiff.status}
              </span>
            )}
          </div>

          {error && <div className="empty-state">Failed to load checkpoints: {error}</div>}

          {!slotA || !slotB ? (
            <div className="empty-state">Drop two checkpoints to compare.</div>
          ) : (
            <>
              {availablePaths.length > 1 && (
                <div className="diff-toolbar">
                  <label htmlFor="diff-path-select" className="diff-toolbar-label">
                    File
                  </label>
                  <select
                    id="diff-path-select"
                    value={comparePath ?? ""}
                    onChange={(e) => handlePathChange(e.target.value)}
                  >
                    {availablePaths.map((p) => (
                      <option key={p} value={p}>
                        {p}
                      </option>
                    ))}
                  </select>
                </div>
              )}

              {pathMismatch && (
                <div className="diff-warning">
                  Comparing different files ({slotA.rel_path} vs {slotB.rel_path}) — showing
                  the diff for {comparePath}.
                </div>
              )}

              {fileDiff && (
                <>
                  <div className="stats-grid" style={{ marginBottom: 16 }}>
                    <div className="stat-card">
                      <div className="stat-label">Total Layers</div>
                      <div className="stat-value">{fileDiff.totalCount}</div>
                    </div>
                    <div className="stat-card">
                      <div className="stat-label">Changed</div>
                      <div className="stat-value accent-amber">{fileDiff.changedCount}</div>
                    </div>
                    <div className="stat-card">
                      <div className="stat-label">% Changed</div>
                      <div className="stat-value accent-orange">
                        {fileDiff.totalCount > 0
                          ? Math.round((fileDiff.changedCount / fileDiff.totalCount) * 100)
                          : 0}
                        %
                      </div>
                    </div>
                    <div className="stat-card">
                      <div className="stat-label">From → To</div>
                      <div className="stat-value" style={{ fontSize: 13 }}>
                        v{slotA.iteration} → v{slotB.iteration}
                      </div>
                      <div className="stat-sub">
                        {shortHash(slotA.commitHash)} → {shortHash(slotB.commitHash)}
                      </div>
                    </div>
                  </div>
                  <WeightHeatmap layers={fileDiff.layers} />
                </>
              )}
            </>
          )}
        </div>

        <div className="card section fade-in fade-in-2">
          <div className="card-title" style={{ marginBottom: 16 }}>
            Layer Drift (changed vs. unchanged by depth)
          </div>
          {fileDiff ? (
            <LayerDriftChart layers={fileDiff.layers} />
          ) : (
            <div className="empty-state">Drop two checkpoints above to compare.</div>
          )}
        </div>
      </div>

      <CheckpointPicker
        rows={rows}
        loading={loading}
        slotA={slotA}
        slotB={slotB}
        onSlotChange={handleSlotChange}
        onResolveHash={(hash, slot) => { void resolveAndFill(hash, slot); }}
        hashLookupError={hashLookupError}
      />
    </div>
  );
}
