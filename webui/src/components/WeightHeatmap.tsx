"use client";

import { formatBytes } from "@/lib/api";
import { MAX_RENDERED_LAYERS, type LayerDiff } from "@/lib/diffWeights";

interface Props {
  layers: LayerDiff[];
}

export function WeightHeatmap({ layers }: Props) {
  if (layers.length === 0) {
    return <div className="empty-state">No per-layer data for this file.</div>;
  }

  const truncated = layers.length > MAX_RENDERED_LAYERS;
  const visible = truncated ? layers.slice(0, MAX_RENDERED_LAYERS) : layers;

  return (
    <div>
      {truncated && (
        <div className="diff-truncate-notice">
          Showing {MAX_RENDERED_LAYERS} of {layers.length} layers
        </div>
      )}
      <div className="weight-heatmap-grid">
        {visible.map((layer) => (
          <div
            key={layer.name}
            className={`layer-cell layer-cell--${layer.status}`}
            title={`${layer.name}\n${layer.status} — ${formatBytes(layer.size)}`}
          />
        ))}
      </div>
    </div>
  );
}
