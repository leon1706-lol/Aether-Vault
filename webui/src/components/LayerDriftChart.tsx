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
import { type LayerDiff, type LayerStatus } from "@/lib/diffWeights";
import { useIncrementalReveal } from "@/hooks/useIncrementalReveal";

interface Props {
  layers: LayerDiff[];
}

// Visualizes *whether* each layer's hash changed across model depth, not a numeric
// weight delta — a real drift would require downloading both full checkpoints.
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
  // v1.3.0 (todo.md item 25): same progressive reveal as WeightHeatmap — see
  // hooks/useIncrementalReveal.ts.
  const visibleCount = useIncrementalReveal(layers.length);

  if (layers.length === 0) {
    return <div className="empty-state">No per-layer data for this file.</div>;
  }

  const rendering = visibleCount < layers.length;
  const visible = layers.slice(0, visibleCount);

  const data = visible.map((layer, idx) => ({
    idx,
    name: layer.name,
    status: layer.status,
    value: layer.status === "unchanged" ? 0 : 1,
  }));

  return (
    <div>
      {rendering && (
        <div className="diff-truncate-notice">
          Rendering {visibleCount} of {layers.length} layers…
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
              formatter={(_value, _name, item) => {
                const payload = (item as { payload?: { name: string; status: string } })?.payload;
                return [payload?.status ?? "", payload?.name ?? ""];
              }}
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
