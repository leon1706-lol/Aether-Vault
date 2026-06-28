import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { StoragePanel } from "../StoragePanel";
import type { Commit } from "@/lib/api";

const { fetchCommit } = vi.hoisted(() => ({ fetchCommit: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchCommit };
});

function makeCommit(overrides: Partial<Commit> = {}): Commit {
  return {
    hash: "a".repeat(64),
    message: "first commit",
    author: "tester",
    timestamp: "2026-06-26T00:00:00Z",
    parent_hash: null,
    root_tree_hash: "b".repeat(64),
    tags: [],
    metrics: {},
    ...overrides,
  };
}

describe("StoragePanel", () => {
  it("shows a loading state when there is no stats response yet", () => {
    render(<StoragePanel stats={null} commits={[]} loading={true} />);
    expect(screen.getByText("Loading storage stats…")).toBeInTheDocument();
  });

  it("renders store-wide headline stats from the dashboard's already-fetched stats", () => {
    render(
      <StoragePanel
        stats={{ total_objects: 42, total_size_bytes: 2048 }}
        commits={[]}
        loading={false}
      />
    );
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("2 KB")).toBeInTheDocument();
  });

  it("buckets the latest commit's tracked files by extension", async () => {
    const commit = makeCommit({
      hash: "a".repeat(64),
      tree: {
        "model.bin": { hash: "x".repeat(64), size: 1000, type: "blob", layers: [] },
        "data.csv": { hash: "y".repeat(64), size: 500, type: "blob", layers: [] },
      },
    });
    fetchCommit.mockResolvedValueOnce(commit);

    render(
      <StoragePanel
        stats={{ total_objects: 2, total_size_bytes: 1500 }}
        commits={[commit]}
        loading={false}
      />
    );

    await waitFor(() => expect(screen.getByText("File-Type Breakdown")).toBeInTheDocument());
    expect(screen.getByText(".bin")).toBeInTheDocument();
    expect(screen.getByText(".csv")).toBeInTheDocument();
    expect(fetchCommit).toHaveBeenCalledWith(commit.hash);
  });

  it("labels the breakdown as latest-snapshot only, not store-wide", () => {
    render(<StoragePanel stats={{ total_objects: 0, total_size_bytes: 0 }} commits={[]} loading={false} />);
    expect(screen.getByText(/latest commit's tracked files/)).toBeInTheDocument();
  });
});
