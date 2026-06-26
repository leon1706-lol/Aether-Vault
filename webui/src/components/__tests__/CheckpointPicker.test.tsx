import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CheckpointPicker, type CheckpointRow } from "../CheckpointPicker";

function row(overrides: Partial<CheckpointRow> = {}): CheckpointRow {
  return {
    iteration: 1,
    commitHash: "a".repeat(64),
    rel_path: "weights/model.safetensors",
    weightHash: "b".repeat(64),
    ...overrides,
  };
}

describe("CheckpointPicker", () => {
  it("shows a loading state when there are no rows yet", () => {
    render(<CheckpointPicker rows={[]} loading={true} slotA={null} slotB={null} onSlotChange={vi.fn()} />);
    expect(screen.getByText("Loading checkpoints…")).toBeInTheDocument();
  });

  it("shows an empty state when loading is done and there are no checkpoints", () => {
    render(<CheckpointPicker rows={[]} loading={false} slotA={null} slotB={null} onSlotChange={vi.fn()} />);
    expect(screen.getByText("No model checkpoints found")).toBeInTheDocument();
  });

  it("clicking a row fills slot A first, then slot B", async () => {
    const user = userEvent.setup();
    const onSlotChange = vi.fn();
    const rows = [
      row({ iteration: 1, rel_path: "weights/model-a.safetensors" }),
      row({ iteration: 2, rel_path: "weights/model-b.safetensors", weightHash: "c".repeat(64) }),
    ];

    render(<CheckpointPicker rows={rows} loading={false} slotA={null} slotB={null} onSlotChange={onSlotChange} />);
    await user.click(screen.getByText("weights/model-a.safetensors"));

    expect(onSlotChange).toHaveBeenCalledWith("A", rows[0]);
  });

  it("clicking a row fills slot B when slot A is already taken", async () => {
    const user = userEvent.setup();
    const onSlotChange = vi.fn();
    const rows = [row({ iteration: 1 }), row({ iteration: 2, rel_path: "weights/model2.safetensors" })];

    render(<CheckpointPicker rows={rows} loading={false} slotA={rows[0]} slotB={null} onSlotChange={onSlotChange} />);
    await user.click(screen.getByText("weights/model2.safetensors"));

    expect(onSlotChange).toHaveBeenCalledWith("B", rows[1]);
  });

  it("clearing a filled slot calls onSlotChange with null", async () => {
    const user = userEvent.setup();
    const onSlotChange = vi.fn();
    const filled = row();

    render(<CheckpointPicker rows={[filled]} loading={false} slotA={filled} slotB={null} onSlotChange={onSlotChange} />);
    await user.click(screen.getByLabelText("Clear Slot A"));

    expect(onSlotChange).toHaveBeenCalledWith("A", null);
  });
});
