// lib/diffWeights.ts — client-side per-layer weight diff
//
// Pure port of python/av_cli/handoff.py's diff_model_weights(): no network calls, operates
// purely on two already-fetched Commit["tree"] objects (the server already returns per-layer
// hashes in GET /api/commits/{hash}, see DBTree.layers in python/av_server/models.py).

import type { Commit, TreeLayer } from "./api";

// Single source of truth for the per-layer shape lives in api.ts (it mirrors the server's
// DBTree.layers JSON column); re-exported here under the name used by the diff types.
export type Layer = TreeLayer;

export type LayerStatus = "changed" | "unchanged" | "added" | "removed";

export interface LayerDiff {
  name: string;
  status: LayerStatus;
  size: number;
}

export type FileStatus = "new" | "removed" | "changed" | "unchanged";

export interface FileDiff {
  rel_path: string;
  status: FileStatus;
  layers: LayerDiff[];
  changedCount: number;
  totalCount: number;
}

// Mirrors python/av_cli/handoff.py:9 (MODEL_EXTS) so the checkpoint picker only surfaces
// actual model artifacts, not datasets or code files.
export const MODEL_EXTS = new Set([".pt", ".pth", ".safetensors", ".onnx", ".ckpt"]);

// Cap applied by the UI when rendering layers (heatmap / drift chart), not by the diff math
// itself — a checkpoint with tens of thousands of tensors must not freeze the page.
export const MAX_RENDERED_LAYERS = 4000;

type Tree = Commit["tree"];

function extOf(relPath: string): string {
  const idx = relPath.lastIndexOf(".");
  return idx === -1 ? "" : relPath.slice(idx).toLowerCase();
}

export function isModelPath(relPath: string): boolean {
  return MODEL_EXTS.has(extOf(relPath));
}

export function listModelPaths(tree: Tree | undefined): string[] {
  if (!tree) return [];
  return Object.keys(tree).filter(isModelPath).sort();
}

// Union of model paths across two trees, for populating a path picker that covers files
// added/removed between the two checkpoints too.
export function unionModelPaths(fromTree: Tree | undefined, toTree: Tree | undefined): string[] {
  return [...new Set([...listModelPaths(fromTree), ...listModelPaths(toTree)])].sort();
}

export function diffFile(fromTree: Tree | undefined, toTree: Tree | undefined, relPath: string): FileDiff {
  const fromEntry = fromTree?.[relPath];
  const toEntry = toTree?.[relPath];

  if (!fromEntry && !toEntry) {
    return { rel_path: relPath, status: "unchanged", layers: [], changedCount: 0, totalCount: 0 };
  }
  if (!fromEntry) {
    return { rel_path: relPath, status: "new", layers: [], changedCount: 0, totalCount: 0 };
  }
  if (!toEntry) {
    return { rel_path: relPath, status: "removed", layers: [], changedCount: 0, totalCount: 0 };
  }

  // aether_core.split_and_hash_safetensors emits a synthetic "__header__" pseudo-layer
  // alongside the real tensors (its hash covers the safetensors JSON header, used for
  // reconstruction integrity) — it is not a model weight, so it is excluded here to avoid
  // polluting the visual diff with a non-tensor entry.
  const fromLayers = (fromEntry.layers ?? []).filter((l) => l.name !== "__header__");
  const toLayers = (toEntry.layers ?? []).filter((l) => l.name !== "__header__");

  if (fromLayers.length > 0 && toLayers.length > 0) {
    const fromByName = new Map(fromLayers.map((l) => [l.name, l]));
    const toByName = new Map(toLayers.map((l) => [l.name, l]));
    const names = [...new Set([...fromByName.keys(), ...toByName.keys()])];

    const layers: LayerDiff[] = names.map((name) => {
      const a = fromByName.get(name);
      const b = toByName.get(name);
      let status: LayerStatus;
      if (!a) status = "added";
      else if (!b) status = "removed";
      else status = a.hash === b.hash ? "unchanged" : "changed";
      return { name, status, size: (b ?? a)?.size ?? 0 };
    });

    const changedCount = layers.filter((l) => l.status !== "unchanged").length;
    return {
      rel_path: relPath,
      status: changedCount > 0 ? "changed" : "unchanged",
      layers,
      changedCount,
      totalCount: layers.length,
    };
  }

  // Whole-file fallback: either side has no per-layer split (small/legacy artifact).
  const changed = fromEntry.hash !== toEntry.hash;
  return {
    rel_path: relPath,
    status: changed ? "changed" : "unchanged",
    layers: [{ name: relPath, status: changed ? "changed" : "unchanged", size: toEntry.size }],
    changedCount: changed ? 1 : 0,
    totalCount: 1,
  };
}
