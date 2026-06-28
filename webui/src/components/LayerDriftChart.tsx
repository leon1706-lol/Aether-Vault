"use client";

import {
  BarChart,
  Bar,
  Cell,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { MAX_RENDERED_LAYERS, type LayerDiff, type LayerStatus } from "@/lib/diffWeights";

interface Props {
  layers: LayerDiff[];
}

// Visualizes *whether* each layer's hash changed across model depth — not a numeric weight
// delta. Computing an actual tensor-value drift (e.g. L2 norm per layer) would require
// downloading both full checkpoints into the browser; this chart only uses the per-layer hash
// equality data the server already exposes (see lib/diffWeights.ts).
const STATUS_COLOR: Record<LayerStatus, string> = {
  unchanged: "#68d391",
  changed: "#fc8181",
  added: "#ff7a1a",
  removed: "#ffd166",
};

const STATUS_LABEL: Record<LayerStatus, string> = {
  unchanged: "Unchanged",
  changed: "Changed",
  added: "Added",
  removed: "Removed",
};

// The Y-axis itself only encodes a binary unchanged(0)/changed(1) value — "changed" covers
// added/removed too, since those also aren't byte-identical to the other side. The legend
// below the chart is what actually distinguishes all 4 statuses (matches the bar colors).
const Y_AXIS_LABEL: Record<number, string> = { 0: "unchanged", 1: "changed" };

export function LayerDriftChart({ layers }: Props) {
  if (layers.length === 0) {
    return <div className="empty-state">No per-layer data for this file.</div>;
  }

  const truncated = layers.length > MAX_RENDERED_LAYERS;
  const visible = truncated ? layers.slice(0, MAX_RENDERED_LAYERS) : layers;

  const data = visible.map((layer, idx) => ({
    idx,
    name: layer.name,
    status: layer.status,
    value: layer.status === "unchanged" ? 0 : 1,
  }));

  return (
    <div>
      {truncated && (
        <div className="diff-truncate-notice">
          Showing {MAX_RENDERED_LAYERS} of {layers.length} layers
        </div>
      )}
      <div className="chart-container" style={{ height: 220 }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 4, right: 12, left: -8, bottom: 24 }}>
            <XAxis
              dataKey="idx"
              tick={false}
              axisLine={{ stroke: "rgba(255,255,255,0.08)" }}
              tickLine={false}
              label={{ value: "Layer depth →", position: "bottom", offset: 6, fill: "#718096", fontSize: 11 }}
            />
            <YAxis
              domain={[0, 1]}
              ticks={[0, 1]}
              tickFormatter={(v: number) => Y_AXIS_LABEL[v] ?? String(v)}
              tick={{ fill: "#718096", fontSize: 11 }}
              axisLine={false}
              tickLine={false}
              width={68}
            />
            <Tooltip
              contentStyle={{
                background: "#0b0f1e",
                border: "1px solid rgba(255,255,255,0.1)",
                borderRadius: 8,
                fontSize: 12,
              }}
              labelStyle={{ color: "#718096" }}
              itemStyle={{ color: "#e2e8f0" }}
              formatter={(_value: number, _name: string, ctx: { payload?: { name: string; status: string } }) => [
                ctx.payload?.status ?? "",
                ctx.payload?.name ?? "",
              ]}
            />
            <Bar dataKey="value" isAnimationActive={false}>
              {data.map((d) => (
                <Cell key={d.idx} fill={STATUS_COLOR[d.status]} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="status-legend">
        {(Object.keys(STATUS_LABEL) as LayerStatus[]).map((status) => (
          <span key={status} className="status-legend-item">
            <span className="status-legend-dot" style={{ background: STATUS_COLOR[status] }} />
            {STATUS_LABEL[status]}
          </span>
        ))}
      </div>
    </div>
  );
}
