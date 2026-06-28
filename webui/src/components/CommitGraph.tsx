"use client";

import { useMemo } from "react";
import { shortHash, type Commit } from "@/lib/api";

interface Props {
  commits: Commit[];
  loading: boolean;
}

// Colors for different branch lanes
const LANE_COLORS = [
  "#ff7a1a", // orange
  "#ffb380", // orange-soft
  "#4fd1c5", // teal
  "#ffd166", // amber
  "#68d391", // green
  "#fc8181", // red
];

const NODE_R = 7;
const COL_W = 22;
const ROW_H = 42;
const PAD_LEFT = 16;
const PAD_TOP = 20;

interface GraphNode {
  commit: Commit;
  col: number;
  row: number;
  x: number;
  y: number;
}

export function buildGraph(commits: Commit[]): { nodes: GraphNode[]; edges: { x1: number; y1: number; x2: number; y2: number; color: string }[] } {
  const sorted = [...commits].sort((a, b) => {
    const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
    const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
    return tb - ta; // newest first = top
  });

  const hashToRow: Record<string, number> = {};
  sorted.forEach((c, i) => { hashToRow[c.hash] = i; });

  // Assign columns: track which column each "stream" is on
  const colByHash: Record<string, number> = {};
  let nextCol = 0;
  const occupiedCols = new Set<number>();

  sorted.forEach((c) => {
    if (colByHash[c.hash] === undefined) {
      colByHash[c.hash] = nextCol;
      occupiedCols.add(nextCol);
      nextCol = Math.min(nextCol + 1, 4); // max 5 lanes
    }
    // Propagate column to parent (if not already assigned)
    if (c.parent_hash && colByHash[c.parent_hash] === undefined) {
      colByHash[c.parent_hash] = colByHash[c.hash];
    }
  });

  const nodes: GraphNode[] = sorted.map((c, row) => {
    const col = colByHash[c.hash] ?? 0;
    return {
      commit: c,
      col,
      row,
      x: PAD_LEFT + col * COL_W,
      y: PAD_TOP + row * ROW_H,
    };
  });

  const nodeByHash: Record<string, GraphNode> = {};
  nodes.forEach((n) => { nodeByHash[n.commit.hash] = n; });

  const edges = nodes
    .filter((n) => n.commit.parent_hash && nodeByHash[n.commit.parent_hash])
    .map((n) => {
      const parent = nodeByHash[n.commit.parent_hash!];
      const color = LANE_COLORS[n.col % LANE_COLORS.length];
      return { x1: n.x, y1: n.y, x2: parent.x, y2: parent.y, color };
    });

  return { nodes, edges };
}

export function CommitGraph({ commits, loading }: Props) {
  const displayCommits = useMemo(() => commits.slice(0, 18), [commits]);
  const { nodes, edges } = useMemo(() => buildGraph(displayCommits), [displayCommits]);

  if (loading && commits.length === 0) {
    return (
      <div className="card">
        <div className="card-header">
          <span className="card-title">
            <GraphIcon />
            Experiment Graph
          </span>
        </div>
        <div className="loading-overlay">
          <div className="spinner" />
          Building graph…
        </div>
      </div>
    );
  }

  if (commits.length === 0) {
    return (
      <div className="card">
        <div className="card-header">
          <span className="card-title">
            <GraphIcon />
            Experiment Graph
          </span>
        </div>
        <div className="empty-state">
          <GraphIcon size={32} />
          <span>No commits to graph</span>
        </div>
      </div>
    );
  }

  const maxCol = Math.max(...nodes.map((n) => n.col), 0);
  const svgW = PAD_LEFT * 2 + (maxCol + 1) * COL_W;
  const svgH = PAD_TOP + nodes.length * ROW_H + PAD_TOP;

  return (
    <div className="card">
      <div className="section-header">
        <span className="card-title">
          <GraphIcon />
          Experiment Graph
        </span>
        <span className="section-count">{commits.length} commits</span>
      </div>

      <div style={{ display: "flex", gap: 0, overflow: "hidden" }}>
        {/* SVG lane graph */}
        <div style={{ flexShrink: 0 }}>
          <svg width={svgW} height={svgH} style={{ overflow: "visible" }}>
            {/* Draw edges first */}
            {edges.map((e, i) => (
              <path
                key={i}
                d={buildEdgePath(e.x1, e.y1, e.x2, e.y2)}
                fill="none"
                stroke={e.color}
                strokeWidth="2"
                opacity="0.5"
              />
            ))}

            {/* Draw nodes */}
            {nodes.map((n) => {
              const color = LANE_COLORS[n.col % LANE_COLORS.length];
              return (
                <g key={n.commit.hash}>
                  {/* Glow */}
                  <circle
                    cx={n.x} cy={n.y} r={NODE_R + 4}
                    fill={color} opacity={0.12}
                  />
                  {/* Node */}
                  <circle
                    cx={n.x} cy={n.y} r={NODE_R}
                    fill={color}
                    stroke="var(--bg-surface)"
                    strokeWidth="2"
                  />
                </g>
              );
            })}
          </svg>
        </div>

        {/* Commit labels */}
        <div
          style={{
            flex: 1,
            minWidth: 0,
            paddingTop: PAD_TOP - NODE_R,
          }}
        >
          {nodes.map((n) => (
            <div
              key={n.commit.hash}
              style={{
                height: ROW_H,
                display: "flex",
                flexDirection: "column",
                justifyContent: "center",
                paddingLeft: 10,
                borderBottom: "1px solid var(--border)",
              }}
            >
              <div
                style={{
                  fontSize: 13,
                  fontWeight: 500,
                  color: "var(--text-primary)",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  maxWidth: "100%",
                }}
                title={n.commit.message}
              >
                {n.commit.message}
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 2 }}>
                <span className="commit-hash">{shortHash(n.commit.hash)}</span>
                <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                  {n.commit.author}
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {commits.length > displayCommits.length && (
        <div
          style={{
            padding: "8px 12px",
            fontSize: 12,
            color: "var(--text-muted)",
            borderTop: "1px solid var(--border)",
          }}
        >
          Showing newest {displayCommits.length} of {commits.length} commits
        </div>
      )}
    </div>
  );
}

function buildEdgePath(
  x1: number, y1: number,
  x2: number, y2: number
): string {
  // Curved bezier connecting commits
  const midY = (y1 + y2) / 2;
  if (x1 === x2) {
    return `M ${x1} ${y1} L ${x2} ${y2}`;
  }
  return `M ${x1} ${y1} C ${x1} ${midY} ${x2} ${midY} ${x2} ${y2}`;
}

function GraphIcon({ size = 16 }: { size?: number }) {
  return (
    <svg
      width={size} height={size}
      viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
    >
      <circle cx="18" cy="5" r="3" />
      <circle cx="6" cy="12" r="3" />
      <circle cx="18" cy="19" r="3" />
      <line x1="8.59" y1="13.51" x2="15.42" y2="17.49" />
      <line x1="15.41" y1="6.51" x2="8.59" y2="10.49" />
    </svg>
  );
}
