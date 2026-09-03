"use client";

import { formatBytes } from "@/lib/api";
import { type LayerDiff } from "@/lib/diffWeights";
import { useIncrementalReveal } from "@/hooks/useIncrementalReveal";

interface Props {
  layers: LayerDiff[];
}

export function WeightHeatmap({ layers }: Props) {
  // v1.3.0 (todo.md item 25): progressive reveal replaces the old hard MAX_RENDERED_LAYERS
  // cutoff — every layer eventually renders, just over a few frames instead of one giant
  // synchronous paint. See hooks/useIncrementalReveal.ts.
  const visibleCount = useIncrementalReveal(layers.length);

  if (layers.length === 0) {
    return <div className="empty-state">No per-layer data for this file.</div>;
  }

  const rendering = visibleCount < layers.length;
  const visible = layers.slice(0, visibleCount);

  return (
    <div>
      {rendering && (
        <div className="diff-truncate-notice">
          Rendering {visibleCount} of {layers.length} layers…
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
