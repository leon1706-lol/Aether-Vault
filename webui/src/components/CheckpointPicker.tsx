"use client";

import { useState } from "react";
import { shortHash } from "@/lib/api";

export interface CheckpointRow {
  iteration: number;
  commitHash: string;
  rel_path: string;
  weightHash: string;
}

export function rowKey(row: CheckpointRow): string {
  return `${row.commitHash}:${row.rel_path}`;
}

interface Props {
  rows: CheckpointRow[];
  loading: boolean;
  slotA: CheckpointRow | null;
  slotB: CheckpointRow | null;
  onSlotChange: (slot: "A" | "B", row: CheckpointRow | null) => void;
  // v1.3.0 (todo.md item 25): arbitrary two-commit compare — the list above only ever
  // shows the most recent CHECKPOINT_FETCH_LIMIT commits (see WeightDiffPanel.tsx); this
  // lets a caller resolve and select an older commit by hash directly. `onResolveHash`
  // resolves (fetching if necessary) and fills the given slot; `hashLookupError` surfaces
  // a failed lookup (unknown hash, or a commit with no model checkpoints) next to the form.
  onResolveHash?: (hash: string, slot: "A" | "B") => void;
  hashLookupError?: string | null;
}

const DRAG_MIME = "application/x-aether-checkpoint";

export function CheckpointPicker({
  rows, loading, slotA, slotB, onSlotChange, onResolveHash, hashLookupError,
}: Props) {
  const [hashInput, setHashInput] = useState("");

  function handleRowClick(row: CheckpointRow) {
    if (!slotA) onSlotChange("A", row);
    else if (!slotB) onSlotChange("B", row);
    else onSlotChange("A", row);
  }

  function handleResolveSubmit(e: React.FormEvent, slot: "A" | "B") {
    e.preventDefault();
    if (!onResolveHash || !hashInput.trim()) return;
    onResolveHash(hashInput.trim(), slot);
    setHashInput("");
  }

  function handleDrop(slot: "A" | "B", e: React.DragEvent) {
    e.preventDefault();
    const raw = e.dataTransfer.getData(DRAG_MIME);
    if (!raw) return;
    try {
      onSlotChange(slot, JSON.parse(raw) as CheckpointRow);
    } catch {
      // Malformed drag payload (e.g. from outside the app) — ignore rather than crash.
    }
  }

  return (
    <div className="card">
      <div className="section-header">
        <span className="card-title">
          <LayersIcon />
          Checkpoints
        </span>
        <span className="section-count">{rows.length}</span>
      </div>

      <div className="diff-slots">
        <DropSlot
          label="Slot A"
          row={slotA}
          onDrop={(e) => handleDrop("A", e)}
          onClear={() => onSlotChange("A", null)}
        />
        <DropSlot
          label="Slot B"
          row={slotB}
          onDrop={(e) => handleDrop("B", e)}
          onClear={() => onSlotChange("B", null)}
        />
      </div>

      {onResolveHash && (
        <form
          className="checkpoint-hash-form"
          onSubmit={(e) => handleResolveSubmit(e, !slotA ? "A" : "B")}
        >
          <label htmlFor="checkpoint-hash-input" className="diff-toolbar-label">
            Compare by hash
          </label>
          <input
            id="checkpoint-hash-input"
            type="text"
            placeholder="commit hash (older than the list below)"
            value={hashInput}
            onChange={(e) => setHashInput(e.target.value)}
          />
          <button type="submit" className="btn btn-ghost" disabled={!hashInput.trim()}>
            Load into {!slotA ? "Slot A" : "Slot B"}
          </button>
        </form>
      )}
      {hashLookupError && <div className="diff-warning">{hashLookupError}</div>}

      {loading && rows.length === 0 ? (
        <div className="loading-overlay">
          <div className="spinner" />
          Loading checkpoints…
        </div>
      ) : rows.length === 0 ? (
        <div className="empty-state">
          <LayersIcon size={28} />
          <span>No model checkpoints found</span>
        </div>
      ) : (
        <div className="checkpoint-list">
          {rows.map((row) => {
            const key = rowKey(row);
            const selected =
              (slotA && rowKey(slotA) === key) || (slotB && rowKey(slotB) === key);
            return (
              <div
                key={key}
                className={`checkpoint-row ${selected ? "checkpoint-row--selected" : ""}`}
                draggable
                onDragStart={(e) => {
                  e.dataTransfer.setData(DRAG_MIME, JSON.stringify(row));
                  e.dataTransfer.effectAllowed = "copy";
                }}
                onClick={() => handleRowClick(row)}
                title={`${row.rel_path} @ ${row.commitHash}`}
              >
                <span className="checkpoint-iter">v{row.iteration}</span>
                <span className="checkpoint-path">{row.rel_path}</span>
                <span className="checkpoint-hash">{shortHash(row.weightHash)}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function DropSlot({
  label,
  row,
  onDrop,
  onClear,
}: {
  label: string;
  row: CheckpointRow | null;
  onDrop: (e: React.DragEvent) => void;
  onClear: () => void;
}) {
  return (
    <div
      className={`diff-slot ${row ? "diff-slot--filled" : ""}`}
      onDragOver={(e) => e.preventDefault()}
      onDrop={onDrop}
    >
      <span className="diff-slot-label">{label}</span>
      {row ? (
        <div className="diff-slot-content">
          <span className="checkpoint-iter">v{row.iteration}</span>
          <span className="checkpoint-path" title={row.rel_path}>
            {row.rel_path}
          </span>
          <button
            type="button"
            className="diff-slot-clear"
            onClick={(e) => {
              e.stopPropagation();
              onClear();
            }}
            aria-label={`Clear ${label}`}
          >
            ✕
          </button>
        </div>
      ) : (
        <span className="diff-slot-placeholder">Drag a checkpoint here, or click one below</span>
      )}
    </div>
  );
}

function LayersIcon({ size = 16 }: { size?: number }) {
  return (
    <svg
      width={size} height={size}
      viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
    >
      <polygon points="12 2 2 7 12 12 22 7 12 2" />
      <polyline points="2 17 12 22 22 17" />
      <polyline points="2 12 12 17 22 12" />
    </svg>
  );
}
