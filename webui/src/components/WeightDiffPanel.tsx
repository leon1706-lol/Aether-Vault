"use client";

import { useEffect, useMemo, useState } from "react";
import { fetchCommitsWithLayers, shortHash, type Commit } from "@/lib/api";
import { diffFile, unionModelPaths, type FileDiff } from "@/lib/diffWeights";
import { CheckpointPicker, type CheckpointRow } from "@/components/CheckpointPicker";
import { WeightHeatmap } from "@/components/WeightHeatmap";
import { LayerDriftChart } from "@/components/LayerDriftChart";

// How many recent commits to eagerly resolve into full trees so the checkpoint list can be
// built. Previously fetched via fetchCommits() + N parallel GET /api/commits/{hash} requests
// (one HTTP round trip per candidate checkpoint); now a single GET /api/commits?include_layers
// request returns all of them with trees already attached (see development/Probleme.md's
// now-fixed "checkpoint list resolves N commits via N parallel requests" entry, and the new
// ?include_layers query param on list_commits in server.py). Raised from the old 30 — that cap
// was specifically there to bound the number of *parallel requests*, which no longer applies
// now that this is one request; kept at 100 to bound response size/render cost instead.
const CHECKPOINT_FETCH_LIMIT = 100;

interface Props {
  // When set, only checkpoints belonging to this project are offered for comparison — keeps
  // an unrelated project's commits (sharing the same registry) out of the picker. When null,
  // checkpoints from every project are shown (matches the dashboard's default behavior).
  projectId?: string | null;
}

export function WeightDiffPanel({ projectId = null }: Props) {
  const [fullCommits, setFullCommits] = useState<Map<string, Commit>>(new Map());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [slotA, setSlotA] = useState<CheckpointRow | null>(null);
  const [slotB, setSlotB] = useState<CheckpointRow | null>(null);
  const [activePath, setActivePath] = useState<string | null>(null);

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

  const rows: CheckpointRow[] = useMemo(() => {
    // Oldest → newest so iteration numbers ("v1, v2, …") increase with history.
    const ordered = [...fullCommits.values()].sort((a, b) => {
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
  }, [fullCommits]);

  function handleSlotChange(slot: "A" | "B", row: CheckpointRow | null) {
    if (slot === "A") setSlotA(row);
    else setSlotB(row);
  }

  const fromTree = slotA ? fullCommits.get(slotA.commitHash)?.tree : undefined;
  const toTree = slotB ? fullCommits.get(slotB.commitHash)?.tree : undefined;

  const availablePaths = useMemo(() => unionModelPaths(fromTree, toTree), [fromTree, toTree]);

  // Default the compared path to whichever slot was filled most recently; fall back to the
  // first available path if that one no longer exists in either tree.
  const comparePath = useMemo(() => {
    if (activePath && availablePaths.includes(activePath)) return activePath;
    return slotB?.rel_path ?? slotA?.rel_path ?? availablePaths[0] ?? null;
  }, [activePath, availablePaths, slotA, slotB]);

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
                    onChange={(e) => setActivePath(e.target.value)}
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
      />
    </div>
  );
}
