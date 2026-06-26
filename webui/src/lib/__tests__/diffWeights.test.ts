import { describe, expect, it } from "vitest";

import type { Commit } from "../api";
import { diffFile, isModelPath, listModelPaths, unionModelPaths } from "../diffWeights";

type Tree = Commit["tree"];

function tree(entries: Record<string, { hash: string; size?: number; layers?: { name: string; hash: string; size: number }[] }>): Tree {
  const out: Tree = {};
  for (const [path, e] of Object.entries(entries)) {
    out[path] = { hash: e.hash, size: e.size ?? 0, type: "artifact", layers: e.layers ?? [] };
  }
  return out;
}

describe("isModelPath", () => {
  it("matches known model extensions, case-insensitively", () => {
    expect(isModelPath("weights/model.pt")).toBe(true);
    expect(isModelPath("weights/MODEL.SAFETENSORS")).toBe(true);
    expect(isModelPath("checkpoint.onnx")).toBe(true);
  });

  it("rejects non-model extensions", () => {
    expect(isModelPath("data/train.csv")).toBe(false);
    expect(isModelPath("src/train.py")).toBe(false);
    expect(isModelPath("README.md")).toBe(false);
  });
});

describe("listModelPaths / unionModelPaths", () => {
  it("filters to only model paths and sorts them", () => {
    const t = tree({
      "b/model.pt": { hash: "h1" },
      "a/train.py": { hash: "h2" },
      "c/weights.safetensors": { hash: "h3" },
    });
    expect(listModelPaths(t)).toEqual(["b/model.pt", "c/weights.safetensors"]);
  });

  it("returns an empty array for an undefined tree", () => {
    expect(listModelPaths(undefined)).toEqual([]);
  });

  it("unions model paths across two trees, deduped and sorted", () => {
    const from = tree({ "model.pt": { hash: "h1" }, "old.onnx": { hash: "h2" } });
    const to = tree({ "model.pt": { hash: "h1" }, "new.safetensors": { hash: "h3" } });
    expect(unionModelPaths(from, to)).toEqual(["model.pt", "new.safetensors", "old.onnx"]);
  });
});

describe("diffFile", () => {
  it("reports unchanged when the path exists in neither tree", () => {
    const result = diffFile(undefined, undefined, "model.pt");
    expect(result).toEqual({ rel_path: "model.pt", status: "unchanged", layers: [], changedCount: 0, totalCount: 0 });
  });

  it("reports new when the path only exists in the target tree", () => {
    const to = tree({ "model.pt": { hash: "h1" } });
    const result = diffFile(undefined, to, "model.pt");
    expect(result.status).toBe("new");
  });

  it("reports removed when the path only exists in the source tree", () => {
    const from = tree({ "model.pt": { hash: "h1" } });
    const result = diffFile(from, undefined, "model.pt");
    expect(result.status).toBe("removed");
  });

  it("whole-file fallback: unchanged when hashes match and neither side has layers", () => {
    const from = tree({ "model.pt": { hash: "h1", size: 100 } });
    const to = tree({ "model.pt": { hash: "h1", size: 100 } });
    const result = diffFile(from, to, "model.pt");
    expect(result.status).toBe("unchanged");
    expect(result.changedCount).toBe(0);
    expect(result.totalCount).toBe(1);
  });

  it("whole-file fallback: changed when hashes differ and neither side has layers", () => {
    const from = tree({ "model.pt": { hash: "h1" } });
    const to = tree({ "model.pt": { hash: "h2" } });
    const result = diffFile(from, to, "model.pt");
    expect(result.status).toBe("changed");
    expect(result.changedCount).toBe(1);
    expect(result.totalCount).toBe(1);
  });

  it("per-layer diff: reports changed/unchanged/added/removed layers correctly", () => {
    const from = tree({
      "model.safetensors": {
        hash: "from-whole",
        layers: [
          { name: "layer1.weight", hash: "a1", size: 10 },
          { name: "layer2.weight", hash: "b2-original", size: 20 },
          { name: "layer3.weight", hash: "c3", size: 30 }, // removed in `to`
        ],
      },
    });
    const to = tree({
      "model.safetensors": {
        hash: "to-whole",
        layers: [
          { name: "layer1.weight", hash: "a1", size: 10 }, // unchanged
          { name: "layer2.weight", hash: "b2-changed", size: 20 }, // changed
          { name: "layer4.weight", hash: "d4", size: 40 }, // added
        ],
      },
    });

    const result = diffFile(from, to, "model.safetensors");
    expect(result.status).toBe("changed");
    expect(result.totalCount).toBe(4);
    expect(result.changedCount).toBe(3); // layer2 changed, layer3 removed, layer4 added

    const byName = new Map(result.layers.map((l) => [l.name, l.status]));
    expect(byName.get("layer1.weight")).toBe("unchanged");
    expect(byName.get("layer2.weight")).toBe("changed");
    expect(byName.get("layer3.weight")).toBe("removed");
    expect(byName.get("layer4.weight")).toBe("added");
  });

  it("per-layer diff: all layers unchanged reports a file-level status of unchanged", () => {
    const from = tree({ "model.safetensors": { hash: "h", layers: [{ name: "l1", hash: "a", size: 5 }] } });
    const to = tree({ "model.safetensors": { hash: "h", layers: [{ name: "l1", hash: "a", size: 5 }] } });
    const result = diffFile(from, to, "model.safetensors");
    expect(result.status).toBe("unchanged");
    expect(result.changedCount).toBe(0);
  });

  it("filters out the synthetic __header__ pseudo-layer from the diff entirely", () => {
    // Regression test for development/Probleme.md's "[3] Synthetic __header__ pseudo-layer
    // pollutes the diff view" — __header__'s hash always changes between versions (it covers
    // the safetensors JSON header) but is not a real tensor and must never appear in the diff.
    const from = tree({
      "model.safetensors": {
        hash: "h",
        layers: [
          { name: "__header__", hash: "header-a", size: 1 },
          { name: "layer1.weight", hash: "a1", size: 10 },
        ],
      },
    });
    const to = tree({
      "model.safetensors": {
        hash: "h2",
        layers: [
          { name: "__header__", hash: "header-b", size: 1 },
          { name: "layer1.weight", hash: "a1", size: 10 },
        ],
      },
    });

    const result = diffFile(from, to, "model.safetensors");
    expect(result.layers.some((l) => l.name === "__header__")).toBe(false);
    expect(result.totalCount).toBe(1);
    expect(result.status).toBe("unchanged");
  });
});
